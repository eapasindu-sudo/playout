"""Commercial clip decoding with PyAV (FFmpeg)."""
import collections
import os
import threading
import queue

import av
import numpy as np

from ndi_io import CHANNELS, SAMPLE_RATE

HWACCEL = "auto"   # "auto", "off", or an FFmpeg device type such as d3d11va / cuda / qsv
_hw_cache = {}


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


MEDIA_EXT = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm", ".wmv"}
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

    VIDEO_BUFFER = 10  # decoded frames held ahead (~80 MB at 1080p)

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
        self._vq = queue.Queue(maxsize=self.VIDEO_BUFFER)
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
        self._thread = threading.Thread(target=self._decode, daemon=True, name=f"decode {self.name}")
        self._thread.start()

    # -- decoder thread -------------------------------------------------
    def _decode(self):
        try:
            with open_video(self.path) as c:
                vs = c.streams.video[0] if c.streams.video else None
                aus = c.streams.audio[0] if c.streams.audio else None
                if vs is None:
                    raise RuntimeError("no video stream")
                vs.thread_type = "AUTO"
                self._has_audio = aus is not None
                resampler = av.AudioResampler(format="flt", layout="stereo", rate=SAMPLE_RATE) if aus else None
                t0 = None
                base = float(vs.start_time * vs.time_base) if vs.start_time is not None and vs.time_base else 0.0
                if self.start > 0:
                    t0 = base + self.start
                    c.seek(int(t0 / vs.time_base), stream=vs, backward=True, any_frame=False)
                end_t = (base + self.end) if self.end else None
                a_skip = None                       # audio samples before the in point still to drop
                a_left = int(self.duration * SAMPLE_RATE) if self.end else None
                streams = [s for s in (vs, aus) if s is not None]
                for packet in c.demux(*streams):
                    if self._stop.is_set():
                        return
                    for frame in packet.decode():
                        if packet.stream is vs:
                            t = frame.time if frame.time is not None else 0.0
                            if t0 is None:
                                t0 = t
                            if t < t0 - self.frame_dur * 0.5:
                                continue            # before the in point (after a seek)
                            if end_t is not None and t >= end_t - self.frame_dur * 0.5:
                                raise _OutPoint()
                            if self.process is not None:
                                item = (t - t0, self.process(frame))
                            else:
                                img = frame.reformat(width=self.width, height=self.height, format="bgra").to_ndarray()
                                item = (t - t0, np.ascontiguousarray(img).reshape(-1))
                            while not self._stop.is_set():
                                try:
                                    self._vq.put(item, timeout=0.1)
                                    break
                                except queue.Full:
                                    continue
                        else:
                            if a_skip is None:
                                at = frame.time
                                a_skip = int(round((t0 - at) * SAMPLE_RATE)) if (self.start > 0 and at is not None) else 0
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
                if resampler:
                    for rf in resampler.resample(None):
                        self._push_audio(rf.to_ndarray().reshape(-1, CHANNELS).T)
        except _OutPoint:
            pass
        except Exception as e:  # bad file: report and let the break move on
            self.error = str(e)
        finally:
            self._done.set()

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
