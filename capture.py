"""Live inputs other than the main vMix NDI feed: USB / HDMI capture devices and test bars.

Source keys used by live cards:
    ""                      the main live input (vMix NDI, chosen on the main screen)
    "ndi:<NDI source name>" another NDI source
    "dev:<video>|<audio>"   a capture device (DirectShow on Windows, V4L2/ALSA on Linux)
    "bars"                  SMPTE colour bars + 1 kHz tone
    "url:<address>"         a network feed: SRT, RTMP, RTSP, HLS (.m3u8), UDP or a YouTube live link
                            srt://0.0.0.0:9000 / rtmp://0.0.0.0:1935/live/key = wait for a sender (listener)
"""
import collections
import re
import shutil
import subprocess
import sys
import threading
import time

import av
import numpy as np

from ndi_io import CHANNELS, SAMPLE_RATE, fit_audio


def make_bars(width, height):
    """SMPTE-style colour bars as a flat BGRA frame."""
    cols = [(192, 192, 192), (0, 192, 192), (192, 192, 0), (0, 192, 0),
            (192, 0, 192), (0, 0, 192), (192, 0, 0)]   # BGR: white yellow cyan green magenta red blue
    img = np.zeros((height, width, 4), np.uint8)
    img[..., 3] = 255
    w = width / 7
    for i, (b, g, r) in enumerate(cols):
        img[: height * 2 // 3, int(i * w):int((i + 1) * w), :3] = (b, g, r)
    rev = [(192, 0, 0), (16, 16, 16), (192, 0, 192), (16, 16, 16), (0, 192, 192), (16, 16, 16), (192, 192, 192)]
    for i, (b, g, r) in enumerate(rev):
        img[height * 2 // 3: height * 3 // 4, int(i * w):int((i + 1) * w), :3] = (b, g, r)
    q = width / 6
    for i, v in enumerate([(106, 33, 0), (235, 235, 235), (107, 0, 50), (16, 16, 16), (16, 16, 16), (16, 16, 16)]):
        img[height * 3 // 4:, int(i * q):int((i + 1) * q), :3] = v
    return img.reshape(-1)


def list_capture_devices(ffmpeg=None):
    """[{name, kind: video|audio}] for capture devices on this PC."""
    out = []
    if sys.platform == "win32":
        exe = ffmpeg or _ffmpeg()
        try:
            r = subprocess.run([exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                               capture_output=True, text=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            for line in r.stderr.splitlines():
                m = re.search(r'"(.+)" \((video|audio)\)', line)
                if m and "Alternative name" not in line:
                    out.append({"name": m.group(1), "kind": m.group(2)})
        except Exception:
            pass
    else:
        import glob
        for dev in sorted(glob.glob("/dev/video*")):
            out.append({"name": dev, "kind": "video"})
    return out


NATIVE = ("blackmagic", "atem", "decklink", "ultrastudio", "intensity", "elgato", "magewell", "aja")


# software "cameras" (vMix / OBS outputs, phone apps...): not shown in the Live input list unless
# switched on in Settings > Live inputs
VIRTUAL = re.compile(r"vmix|obs virtual|virtual|droidcam|xsplit|snap camera|nvidia broadcast|imaging edge|"
                     r"manycam|splitcam|epoccam|ndi webcam|newtek|iriun|camo|streamlabs|wirecast", re.I)


def default_shown(name):
    return not VIRTUAL.search(name or "")


class DeviceWatch:
    """Looks for USB capture devices every few seconds, so a device plugged in shows up by itself."""

    def __init__(self, ffmpeg=None, every=5.0):
        self.ffmpeg, self.every = ffmpeg, every
        self.devices = []
        self.changed = 0.0             # when the list last changed
        self.on_added = None           # fn(names of video devices plugged in since the last look)
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="usb devices").start()

    def scan(self):
        found = list_capture_devices(self.ffmpeg)
        added = []
        with self._lock:
            if found != self.devices:
                old = {d["name"] for d in self.devices if d["kind"] == "video"}
                added = [d["name"] for d in found if d["kind"] == "video" and d["name"] not in old] if self.changed else []
                self.devices = found
                self.changed = time.time()
        if added and self.on_added:
            self.on_added(added)
        return found

    def _run(self):
        while True:
            try:
                self.scan()
            except Exception:
                pass
            time.sleep(self.every)

    def get(self):
        with self._lock:
            return list(self.devices)


def device_modes(width, height, fps, name=""):
    """Capture formats to ask a USB device for, best first. Many cheap USB HDMI grabbers give 1080p only at
    5 fps uncompressed but 25 / 30 / 60 fps as MJPEG, so MJPEG is asked for first; the ATEM Mini (and other
    UVC devices without MJPEG) then open with the size and frame rate, and the last try takes the device default."""
    rate = f"{float(fps):g}"
    size = f"{width}x{height}"
    if any(k in name.lower() for k in NATIVE):
        # broadcast devices (Blackmagic ATEM in USB mode...): the picture as the device sends it, at
        # Playout's size and frame rate when the device offers it (an ATEM sending 1080p60 into a 25 fps
        # output means 35 pictures a second decoded for nothing); otherwise its own default
        return [("native", {"video_size": size, "framerate": rate}), ("native", {})] + \
            [m for m in device_modes(width, height, fps) if m[0] != "default"]
    return [
        ("mjpeg", {"vcodec": "mjpeg", "video_size": size, "framerate": rate}),
        ("raw", {"video_size": size, "framerate": rate}),
        ("mjpeg", {"vcodec": "mjpeg", "video_size": size}),
        ("raw", {"video_size": size}),
        ("default", {}),
    ]


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


def open_args(key):
    """(url, format, options) for PyAV from a source key."""
    if key == "bars":
        return None
    if key.startswith("dev:"):
        video, _, audio = key[4:].partition("|")
        if sys.platform == "win32":
            url = f"video={video}" + (f":audio={audio}" if audio else "")
            # audio_buffer_size: small sound packets (the default is 500 ms blocks, which arrive late and in bursts)
            # rtbufsize: room for a short hiccup only; a big buffer fills up when the PC is busy and then
            # the picture runs seconds behind (the "delay" on USB capture)
            return url, "dshow", {"rtbufsize": "64M", "audio_buffer_size": "40"}
        return video, "v4l2", {}
    if key.startswith("lavfi:"):
        return key[6:], "lavfi", {}
    raise ValueError(f"unknown source {key}")


class CaptureInput:
    """Reads a capture device on its own thread; the output loop takes the newest frame."""

    MAX_AUDIO = SAMPLE_RATE * 2 // 5  # safety: never more than 400 ms of sound queued
    TARGET_AUDIO = SAMPLE_RATE // 10  # (safety trim back to 100 ms)
    PREBUF = SAMPLE_RATE // 10        # 100 ms cushion: USB sound comes in uneven bursts; playing from a
                                      # small cushion means no gaps (clicks) when a burst is a little late
    MAX_FRAMES = 2                    # video frames held to smooth out uneven USB delivery

    def __init__(self, key, width, height, fps=25):
        self.key = key
        self.width, self.height = width, height
        self.fps = fps
        self.source_name = key
        self.error = ""
        self.mode = ""                # what the device opened as, e.g. "1920x1080 25 fps mjpeg + sound"
        self.in_fps = None            # frames per second really coming from the device
        self.dropped = 0              # old pictures skipped to stay in time
        self._video = None
        self._frames = collections.deque()
        self._audio = collections.deque()
        self._audio_len = 0
        self._playing = False         # sound cushion filled, playing
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_frame_time = 0.0
        if key == "bars":
            self._video = self._make_bars()
            self._tone_phase = 0
        else:
            threading.Thread(target=self._run, daemon=True, name=f"capture {key}").start()

    @property
    def connected(self):
        if self.key == "bars":
            return True
        return time.time() - self._last_frame_time < 1.0

    def _make_bars(self):
        return make_bars(self.width, self.height)

    def _open(self):
        """Open the device, trying the capture formats from best to plain; with the sound device if possible."""
        url, fmt, opts = open_args(self.key)
        if fmt != "dshow":
            return av.open(url, format=fmt, options=opts, timeout=5), "", ""
        video, _, audio = self.key[4:].partition("|")
        urls = [url] + ([f"video={video}"] if audio else [])
        last = None
        for u in urls:
            for name, extra in device_modes(self.width, self.height, self.fps, video):
                try:
                    c = av.open(u, format=fmt, options={**opts, **extra}, timeout=5)
                    note = "" if u == url or not audio else f"sound device {audio} did not open"
                    return c, name, note
                except Exception as e:
                    last = e
        raise last or RuntimeError("cannot open the device")

    def _run(self):
        while not self._stop.is_set():
            try:
                c, mode, note = self._open()
                with c:
                    self.error = note
                    vs = c.streams.video[0] if c.streams.video else None
                    aus = c.streams.audio[0] if c.streams.audio else None
                    if vs is not None:
                        vs.thread_type = "AUTO"
                        cc = vs.codec_context
                        rate = vs.average_rate or vs.guessed_rate
                        self.mode = (f"{cc.width}x{cc.height}" + (f" {float(rate):g} fps" if rate else "") +
                                     (f" {cc.name}" if cc.name else "") + (" + sound" if aus else " (no sound)"))
                    resampler = av.AudioResampler(format="flt", layout="stereo", rate=SAMPLE_RATE) if aus else None
                    count, t0 = 0, time.time()
                    gap = 0.9 / float(self.fps)         # keep at most one picture per output frame
                    kept, behind = -1e9, None            # (time of the last picture kept, clock offset)
                    self.dropped = 0
                    for packet in c.demux(*[s for s in (vs, aus) if s is not None]):
                        if self._stop.is_set():
                            return
                        for frame in packet.decode():
                            if packet.stream is vs:
                                now = time.time()
                                self._last_frame_time = now
                                count += 1
                                if now - t0 >= 2:
                                    self.in_fps = round(count / (now - t0), 1)
                                    count, t0 = 0, now
                                # running behind (the PC was busy): skip old pictures until caught up,
                                # so the delay never builds up
                                if frame.time is not None:
                                    off = now - frame.time
                                    behind = off if behind is None or off < behind else behind + (off - behind) * 0.001
                                    if off - behind > 0.12:
                                        self.dropped += 1
                                        continue
                                ft = frame.time if frame.time is not None else now
                                if ft - kept < gap:      # device sends faster than the output uses
                                    continue
                                kept = ft
                                img = frame.reformat(width=self.width, height=self.height, format="bgra",
                                                     interpolation="FAST_BILINEAR").to_ndarray()
                                with self._lock:
                                    self._frames.append(np.ascontiguousarray(img).reshape(-1))
                                    while len(self._frames) > self.MAX_FRAMES:
                                        self._frames.popleft()
                            elif resampler is not None:
                                for rf in resampler.resample(frame):
                                    self._push_audio(rf.to_ndarray().reshape(-1, CHANNELS).T)
            except Exception as e:
                self.error = str(e)
                self.mode, self.in_fps = "", None
            if not self._stop.wait(2.0):   # device unplugged or busy: try again
                continue

    def _push_audio(self, pcm):
        with self._lock:
            self._audio.append(np.ascontiguousarray(pcm, dtype=np.float32))
            self._audio_len += pcm.shape[1]
            while self._audio_len > self.MAX_AUDIO and self._audio:
                drop = self._audio_len - self.TARGET_AUDIO
                first = self._audio[0]
                if first.shape[1] <= drop:
                    self._audio.popleft()
                    self._audio_len -= first.shape[1]
                else:
                    self._audio[0] = first[:, drop:]
                    self._audio_len -= drop

    def grab(self, n_samples, want_video=True):
        if self.key == "bars":
            t = (np.arange(n_samples) + self._tone_phase) / SAMPLE_RATE
            self._tone_phase += n_samples
            tone = (0.1 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)   # -20 dBFS
            return self._video, np.vstack([tone, tone])
        with self._lock:
            # one new frame per output frame, in order: a frame that comes a little early or late from the
            # USB device still shows for its own frame time (no jumping back and forth)
            if self._frames:
                self._video = self._frames.popleft()
            video = self._video
            out = self._take_audio(n_samples)
        if not self.connected:
            video = None
        return video, fit_audio(out, n_samples)

    def _pop(self, k):
        """Up to k queued sound samples (lock held)."""
        parts, got = [], 0
        while got < k and self._audio:
            chunk = self._audio[0]
            take = min(k - got, chunk.shape[1])
            parts.append(chunk[:, :take])
            if take == chunk.shape[1]:
                self._audio.popleft()
            else:
                self._audio[0] = chunk[:, take:]
            got += take
        self._audio_len -= got
        return np.concatenate(parts, axis=1) if parts else np.zeros((CHANNELS, 0), np.float32)

    def _take_audio(self, n):
        """n samples of sound for one output frame, smooth even when the device clock runs a little
        fast or slow against the output: the cushion is kept near 80 ms by playing up to 1% faster or
        slower (not audible), instead of cutting pieces out (clicks) or running dry (gaps)."""
        if not self._playing:
            if self._audio_len < self.PREBUF:
                return np.zeros((CHANNELS, n), np.float32)
            self._playing = True
        extra = (self._audio_len - self.PREBUF) / SAMPLE_RATE      # seconds away from the cushion size
        step = 0.0 if abs(extra) < 0.01 else 0.005 if abs(extra) < 0.03 else 0.01
        k = n + int(round(n * step)) * (1 if extra > 0 else -1)
        if self._audio_len < k:               # ran dry: fade out what is there, build the cushion again
            a = self._pop(self._audio_len)
            self._playing = False
            out = np.zeros((CHANNELS, n), np.float32)
            if a.shape[1]:
                m = a.shape[1]
                out[:, :m] = fit_audio(a, m) * np.linspace(1, 0, m, dtype=np.float32)[None, :]
            return out
        a = fit_audio(self._pop(k), k)
        if k != n:
            src = np.linspace(0, k - 1, n)
            a = np.vstack([np.interp(src, np.arange(k), a[c]) for c in range(a.shape[0])]).astype(np.float32)
        return a

    def close(self):
        self._stop.set()


def _resolve_youtube(url):
    """Direct stream address of a YouTube (or Facebook) live page, via yt-dlp."""
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": "best[height<=1080]/best"}) as y:
        info = y.extract_info(url, download=False)
    return info.get("url") or info["formats"][-1]["url"]


def url_command(ffmpeg, url):
    """ffmpeg command that receives a network feed and passes it on, unchanged, as MPEG-TS on stdout."""
    low = url.lower()
    pre = ["-hide_banner", "-loglevel", "error", "-fflags", "+nobuffer+genpts", "-probesize", "2M",
           "-analyzeduration", "2M"]
    if low.startswith("rtmp://0.0.0.0") or low.startswith("rtmp://localhost"):
        pre += ["-listen", "1", "-timeout", "-1"]
    elif low.startswith("srt://") and "mode=" not in low:
        host = low[6:].split(":")[0]
        url += ("&" if "?" in url else "?") + ("mode=listener" if host in ("", "0.0.0.0") else "mode=caller")
    elif low.startswith("rtsp://"):
        pre += ["-rtsp_transport", "tcp"]
    elif low.startswith(("http://", "https://")) and not low.split("?")[0].endswith((".m3u8", ".ts", ".flv", ".mp4")):
        url = _resolve_youtube(url)
    elif not low.startswith(("http", "udp://", "rtp://", "srt://", "rtmp", "rtsp")):
        pre += ["-re"]           # a file: play it at normal speed (loops in the card)
    return [ffmpeg] + pre + ["-i", url, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "mpegts", "pipe:1"]


class UrlInput(CaptureInput):
    """Network feed as a live card source (FFmpeg receives it, PyAV decodes it)."""

    def __init__(self, key, width, height, ffmpeg=None):
        self.ffmpeg = ffmpeg or _ffmpeg()
        self._proc = None
        super().__init__(key, width, height)

    def _run(self):
        url = self.key[4:]
        while not self._stop.is_set():
            proc = None
            try:
                cmd = url_command(self.ffmpeg, url)
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                proc = self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                     stdin=subprocess.DEVNULL, creationflags=flags)
                threading.Thread(target=self._read_err, args=(proc,), daemon=True).start()
                with av.open(proc.stdout, format="mpegts", options={"fflags": "nobuffer"}) as c:
                    self.error = ""
                    vs = c.streams.video[0] if c.streams.video else None
                    aus = c.streams.audio[0] if c.streams.audio else None
                    if vs is not None:
                        vs.thread_type = "AUTO"
                    resampler = av.AudioResampler(format="flt", layout="stereo", rate=SAMPLE_RATE) if aus else None
                    for packet in c.demux(*[s for s in (vs, aus) if s is not None]):
                        if self._stop.is_set():
                            break
                        for frame in packet.decode():
                            if packet.stream is vs:
                                img = frame.reformat(width=self.width, height=self.height, format="bgra",
                                                     interpolation="FAST_BILINEAR").to_ndarray()
                                with self._lock:
                                    self._video = np.ascontiguousarray(img).reshape(-1)
                                self._last_frame_time = time.time()
                            elif resampler is not None:
                                for rf in resampler.resample(frame):
                                    self._push_audio(rf.to_ndarray().reshape(-1, CHANNELS).T)
            except Exception as e:
                if not self.error:
                    self.error = str(e)
            finally:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if self._stop.wait(2.0):
                return

    def _read_err(self, proc):
        for line in proc.stderr:
            t = line.decode("utf-8", "replace").strip()
            if t:
                self.error = t[-200:]

    def close(self):
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
