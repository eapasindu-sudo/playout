"""Commercial clip decoding with PyAV (FFmpeg)."""
import collections
import logging
import math
import os
import struct
import threading
import queue
import time

import av
import numpy as np

import gpucg

from ndi_io import CHANNELS, SAMPLE_RATE

HWACCEL = "auto"   # "auto", "off", or an FFmpeg device type such as d3d11va / cuda / qsv
_hw_cache = {}
DECODE = {"gpu": None, "codec": ""}   # how the last clip is being decoded (health page)


def _hwaccel():
    """GPU video decoding (keeps CPU use down). Falls back to software decoding by itself."""
    mode = (HWACCEL or "auto").lower()
    if mode in ("off", "none", "cpu", ""):
        return None
    if mode in _hw_cache:
        return _hw_cache[mode]
    accel = None
    try:
        from av.codec.hwaccel import HWAccel, hwdevices_available
        have = hwdevices_available()
        order = [mode] if mode != "auto" else ["d3d11va", "cuda", "qsv", "dxva2", "videotoolbox", "vaapi"]
        for dev in order:
            if dev in have:
                accel = HWAccel(device_type=dev, allow_software_fallback=True)
                DECODE["device"] = dev
                break
    except Exception:
        accel = None
    _hw_cache[mode] = accel
    return accel


def open_video(path):
    """av.open with GPU decoding when the PC has it, plain software decoding otherwise."""
    accel = _hwaccel()
    if accel is not None:
        try:
            return av.open(path, hwaccel=accel)
        except Exception:
            pass
    return av.open(path)


MEDIA_EXT = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".m2v", ".vob",
             ".webm", ".wmv", ".m4v", ".flv", ".3gp", ".dv", ".ogv", ".mjpeg", ".y4m"}
_probe_cache = {}


def probe(path):
    """Duration in seconds (cached by path + mtime)."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key in _probe_cache:
        return _probe_cache[key]
    dur = None
    try:
        with av.open(path) as c:
            if c.duration:
                dur = c.duration / 1_000_000
            elif c.streams.video and c.streams.video[0].duration:
                s = c.streams.video[0]
                dur = float(s.duration * s.time_base)
    except Exception:
        dur = None
    _probe_cache[key] = dur
    return dur


def list_media(folder):
    out = []
    for name in sorted(os.listdir(folder), key=str.lower):
        p = os.path.join(folder, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in MEDIA_EXT:
            out.append({"name": name, "duration": probe(p)})
    return out


class _OutPoint(Exception):
    """Decoding reached the clip's out point."""


class ClipPlayer:
    """Decodes one clip ahead of time on a background thread.

    The output loop calls next_frame(n_samples) once per output frame; the
    player picks the video frame whose timestamp matches the output clock
    (so 25p, 30p, 50p clips all play at the right speed) and hands back
    exactly n_samples of 48 kHz stereo audio.
    """

    VIDEO_BUFFER = 8   # decoded frames held ahead at 1080p (~65 MB); fewer at 4K, so RAM stays the same

    def __init__(self, path, width, height, fps, process=None, start=0.0, end=None, gain=1.0):
        self.path = path
        self.process = process   # optional: called with each decoded VideoFrame (CG effects)
        self.name = os.path.basename(path)
        self.width, self.height = width, height
        self.frame_dur = 1.0 / float(fps)
        full = probe(path) or 0.0
        self.start = max(0.0, float(start or 0))          # trim: in point (seconds into the file)
        self.end = float(end) if end and (not full or float(end) < full) else None   # out point
        self.gain = float(gain or 1.0)                    # audio level (normalize + item volume)
        self.duration = max(0.0, (self.end or full) - self.start) if full or self.end else 0.0
        self.error = None
        self._vq = queue.Queue(maxsize=max(3, int(self.VIDEO_BUFFER * 1920 * 1080 / max(1, width * height))))
        self._audio = collections.deque()
        self._audio_len = 0
        self._alock = threading.Lock()
        self._done = threading.Event()
        self._stop = threading.Event()
        self._has_audio = False
        self._current = None
        self._pending = None
        self._last_t = 0.0
        self.tick = 0
        self._t_offset = 0.0              # times of a decoder started part-way (GPU engine stopped mid-clip)
        import gpuengine
        target = self._decode_native if process is None and gpuengine.active() is not None else self._decode
        self._thread = threading.Thread(target=target, daemon=True, name=f"decode {self.name}")
        self._thread.start()

    # -- decoder thread -------------------------------------------------
    def _decode_native(self):
        """The GPU engine decodes the picture on the graphics card (d3d11va) and makes it BGRA at the output size
        there; here the ready pictures are only taken from its shared memory. The sound is decoded here as always.
        If the engine cannot (or stops), the picture is decoded here from the same point on."""
        import gpuengine
        nat = gpuengine.active()
        W, H = self.width, self.height
        clip = nat.clip_open(self.path, W, H, self.start, self.end or 0, self.frame_dur) if nat is not None else None
        if clip is None:
            return self._decode()
        threading.Thread(target=self._decode_audio, daemon=True, name=f"audio {self.name}").start()
        self._has_audio = True
        mm, size = clip.mm, W * H * 4
        slot, got, last_t = 0, False, 0.0
        fallback = False
        try:
            while not self._stop.is_set():
                if struct.unpack_from("<i", mm, 64 + slot * 16)[0] == 1:
                    t = struct.unpack_from("<d", mm, 64 + slot * 16 + 8)[0]
                    img = np.frombuffer(mm, np.uint8, size, gpuengine.CLIP_PIXELS + slot * size).copy()
                    struct.pack_into("<i", mm, 64 + slot * 16, 0)
                    slot = (slot + 1) % gpuengine.CLIP_SLOTS
                    if not got:
                        got = True
                        DECODE["gpu"] = clip.mode.startswith("gpu")
                        DECODE["codec"] = clip.mode.partition(" ")[2] + " (GPU engine)"
                    last_t = t
                    while not self._stop.is_set():
                        try:
                            self._vq.put((t, img), timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    continue
                if clip.done.is_set():
                    if struct.unpack_from("<i", mm, 64 + slot * 16)[0] == 1:
                        continue                      # pictures written just before the end
                    if clip.error:
                        fallback = True
                        logging.info("clip %s on the GPU engine: %s (decoding here)", self.name, clip.error)
                    break
                if not nat.ready:
                    fallback = True
                    break
                time.sleep(0.003)
        finally:
            clip.close()
        if fallback and not self._stop.is_set():
            if got:
                self._t_offset = last_t + self.frame_dur
                self.start += self._t_offset
            return self._decode(audio=False)
        self._done.set()

    def _decode(self, audio=True):
        try:
            with open_video(self.path) as c:
                vs = c.streams.video[0] if c.streams.video else None
                aus = c.streams.audio[0] if c.streams.audio else None
                if vs is None:
                    raise RuntimeError("no video stream")
                vs.thread_type = "AUTO"
                hw = bool(getattr(vs.codec_context, "is_hwaccel", False))
                DECODE["codec"] = vs.codec_context.name
                checked = False
                self._has_audio = aus is not None
                t0 = None
                base = float(vs.start_time * vs.time_base) if vs.start_time is not None and vs.time_base else 0.0
                if self.start > 0:
                    t0 = base + self.start
                    c.seek(int(t0 / vs.time_base), stream=vs, backward=True, any_frame=False)
                end_t = (base + self.end) if self.end else None
                last_slot = None
                if aus is not None and audio:
                    # sound has its own reader, seconds ahead of the picture: when the PC is busy for a
                    # moment (a web overlay redrawing, a GPU stall) the pictures fall a few frames behind
                    # but the sound never runs dry (it used to wait behind the full picture queue)
                    threading.Thread(target=self._decode_audio, daemon=True, name=f"audio {self.name}").start()
                for packet in c.demux(vs):
                    if self._stop.is_set():
                        return
                    for frame in packet.decode():
                        if packet.stream is vs:
                            if not checked:            # GPU frames come back as nv12 / p010
                                checked = True
                                DECODE["gpu"] = hw and frame.format.name in ("nv12", "p010le", "p016le")
                            t = frame.time if frame.time is not None else 0.0
                            if t0 is None:
                                t0 = t
                            if t < t0 - self.frame_dur * 0.5:
                                continue            # before the in point (after a seek)
                            if end_t is not None and t >= end_t - self.frame_dur * 0.5:
                                raise _OutPoint()
                            # one picture per output frame: a 50p / 60p clip at 25p would otherwise convert
                            # every picture and the output would throw half of them away
                            slot = math.ceil((t - t0) / self.frame_dur - 0.5 - 1e-6)
                            if slot == last_slot:
                                continue
                            last_slot = slot
                            if self.process is not None:
                                item = (t - t0 + self._t_offset, self.process(frame))
                            else:
                                item = (t - t0 + self._t_offset, gpucg.to_bgra(frame, self.width, self.height))
                            while not self._stop.is_set():
                                try:
                                    self._vq.put(item, timeout=0.1)
                                    break
                                except queue.Full:
                                    continue
        except _OutPoint:
            pass
        except Exception as e:  # bad file: report and let the break move on
            self.error = str(e)
        finally:
            self._done.set()

    AUDIO_AHEAD = 4 * SAMPLE_RATE   # sound decoded ahead of the output (~1.5 MB)

    def _decode_audio(self):
        try:
            with av.open(self.path) as c:
                aus = c.streams.audio[0]
                vs = c.streams.video[0] if c.streams.video else None
                # the in point is measured from the video's start, as the picture side does
                ref = vs if vs is not None and vs.start_time is not None and vs.time_base else aus
                base = float(ref.start_time * ref.time_base) if ref.start_time is not None and ref.time_base else 0.0
                t0 = base + self.start if self.start > 0 else None
                if t0 is not None:
                    c.seek(int(t0 / aus.time_base), stream=aus, backward=True, any_frame=False)
                resampler = av.AudioResampler(format="flt", layout="stereo", rate=SAMPLE_RATE)
                a_skip = None                       # samples before the in point still to drop
                a_left = int(self.duration * SAMPLE_RATE) if self.end else None
                for packet in c.demux(aus):
                    for frame in packet.decode():
                        if a_skip is None:
                            at = frame.time
                            a_skip = int(round((t0 - at) * SAMPLE_RATE)) if (t0 is not None and at is not None) else 0
                        for rf in resampler.resample(frame):
                            pcm = rf.to_ndarray().reshape(-1, CHANNELS).T
                            if a_skip > 0:
                                cut = min(a_skip, pcm.shape[1])
                                pcm, a_skip = pcm[:, cut:], a_skip - cut
                            if a_left is not None:
                                pcm = pcm[:, :max(0, a_left)]
                                a_left -= pcm.shape[1]
                            if pcm.shape[1]:
                                self._push_audio(pcm)
                    while self._audio_len > self.AUDIO_AHEAD and not self._stop.is_set():
                        time.sleep(0.02)
                    if self._stop.is_set() or (a_left is not None and a_left <= 0):
                        return
                for rf in resampler.resample(None):
                    self._push_audio(rf.to_ndarray().reshape(-1, CHANNELS).T)
        except Exception as e:
            logging.info("clip sound %s: %s", self.name, e)

    def _push_audio(self, pcm):
        if self.gain != 1.0:
            pcm = np.clip(pcm * np.float32(self.gain), -1.0, 1.0)
        with self._alock:
            self._audio.append(np.ascontiguousarray(pcm, dtype=np.float32))
            self._audio_len += pcm.shape[1]

    def _pull_audio(self, n):
        out = np.zeros((CHANNELS, n), np.float32)
        got = 0
        with self._alock:
            while got < n and self._audio:
                chunk = self._audio[0]
                take = min(n - got, chunk.shape[1])
                out[:, got:got + take] = chunk[:, :take]
                if take == chunk.shape[1]:
                    self._audio.popleft()
                else:
                    self._audio[0] = chunk[:, take:]
                got += take
                self._audio_len -= take
        return out

    # -- output side ----------------------------------------------------
    def ready(self):
        """True once the first frame is decoded (or the clip failed)."""
        return self._vq.qsize() > 0 or self._done.is_set()

    def finished(self):
        if self.error and self._current is None:
            return True
        return (self._done.is_set() and self._vq.empty() and self._pending is None
                and self.tick * self.frame_dur >= self._last_t + self.frame_dur * 0.5)

    def next_frame(self, n_samples):
        t = self.tick * self.frame_dur
        # advance to the newest decoded frame that is due at time t
        while True:
            if self._pending is None:
                try:
                    self._pending = self._vq.get_nowait()
                except queue.Empty:
                    break
            if self._pending[0] <= t + self.frame_dur * 0.5:
                self._last_t, self._current = self._pending
                self._pending = None
            else:
                break
        self.tick += 1
        audio = self._pull_audio(n_samples)
        return self._current, audio

    @property
    def position(self):
        return self.tick * self.frame_dur

    @property
    def remaining(self):
        return max(0.0, self.duration - self.position)

    def close(self):
        self._stop.set()


def thumbnail(path, cache_dir, width=192):
    """JPEG thumbnail (frame at 2 s, or the middle of a shorter clip), cached by path + mtime."""
    from PIL import Image
    os.makedirs(cache_dir, exist_ok=True)
    key = f"{os.path.basename(path)}.{int(os.path.getmtime(path))}.{width}.t2.jpg"
    out = os.path.join(cache_dir, key)
    if os.path.exists(out):
        return out
    img = None
    with av.open(path) as c:
        vs = c.streams.video[0]
        target = 2.0
        if c.duration and c.duration / 1_000_000 < 2.5:
            target = c.duration / 1_000_000 / 2
        base = float(vs.start_time * vs.time_base) if vs.start_time is not None and vs.time_base else 0.0
        for frame in c.decode(vs):
            img = frame.to_image()
            if frame.time is None or frame.time - base >= target:
                break
    if img is None:
        raise RuntimeError("no frames")
    img = img.convert("RGB")
    img.thumbnail((width, width * 9 // 16 + 1))
    img.save(out, "JPEG", quality=75)
    return out
