"""NDI input (vMix program feed) and NDI output, built on cyndilib."""
import collections
import threading
import time
from fractions import Fraction

import numpy as np
from cyndilib import Finder, Receiver, Sender
from cyndilib.audio_frame import AudioFrameSync, AudioSendFrame
from cyndilib.video_frame import VideoFrameSync, VideoSendFrame
from cyndilib.wrapper.ndi_recv import RecvBandwidth, RecvColorFormat
from cyndilib.wrapper.ndi_structs import FourCC

SAMPLE_RATE = 48000
CHANNELS = 2


class SourceFinder:
    """Keeps a running list of NDI sources on the network."""

    def __init__(self):
        self.finder = Finder()
        self.finder.open()

    def names(self):
        self.finder.update_sources()
        return sorted(self.finder.get_source_names())

    def details(self):
        """[{name, host, stream}] of every source found on the network (NDI Sources panel)."""
        self.finder.update_sources()
        out = []
        for src in list(self.finder.iter_sources()):
            try:
                out.append({"name": src.name, "host": src.host_name, "stream": src.stream_name})
            except Exception:
                out.append({"name": src.name, "host": "", "stream": ""})
        return sorted(out, key=lambda d: d["name"].lower())

    def get(self, name):
        self.finder.update_sources()
        if name in self.finder.get_source_names():
            return self.finder.get_source(name)
        return None

    def close(self):
        self.finder.close()


class LiveInput:
    """Receives one NDI source through an NDI FrameSync.

    FrameSync gives us the latest video frame and exactly the number of audio
    samples we ask for, time-corrected to *our* output clock. That is what a
    playout server needs: the output never stalls if vMix hiccups.
    """

    def __init__(self, finder: SourceFinder, width: int, height: int):
        self.finder = finder
        self.width, self.height = width, height
        self.source_name = None
        self.receiver = None
        self.video = None
        self.audio = None
        self._lock = threading.Lock()
        self._last_try = 0.0
        self.tally = (False, False)      # (program, preview) we want the source to show
        self._sent_tally = None
        self.audio_pair = 0              # which channel pair to take: 0 = 1-2, 1 = 3-4, ...
        self.keep_alpha = False          # True: hand the alpha channel on (NDI key overlay)
        # what the source sends (NDI Sources panel / health): filled while frames come in
        self.fmt = {}
        self.in_fps = None
        self.dropped = 0
        self._stat_t = 0.0
        self._stat_total = None

    def set_source(self, name):
        with self._lock:
            self._close()
            self.source_name = name or None
            self._last_try = 0.0

    def _close(self):
        if self.receiver is not None:
            try:
                self.receiver.disconnect()
            except Exception:
                pass
        self.receiver = None
        self._sent_tally = None
        self.fmt, self.in_fps, self._stat_total = {}, None, None

    def _ensure(self):
        if self.receiver is not None or not self.source_name:
            return
        now = time.monotonic()
        if now - self._last_try < 2.0:
            return
        self._last_try = now
        src = self.finder.get(self.source_name)
        if src is None:
            return
        r = Receiver(color_format=RecvColorFormat.BGRX_BGRA,
                     bandwidth=RecvBandwidth.highest,
                     recv_name="Playout Live In")
        self.video = VideoFrameSync()
        self.audio = AudioFrameSync()
        r.frame_sync.set_video_frame(self.video)
        r.frame_sync.set_audio_frame(self.audio)
        r.set_source(src)
        self.receiver = r

    @property
    def connected(self):
        r = self.receiver
        try:
            return bool(r is not None and r.is_connected())
        except Exception:
            return False

    def grab(self, n_samples, want_video=True):
        """Return (video, audio) for one output frame.

        video is a flat uint8 BGRA buffer of the output size, or None when
        there is no usable picture. audio is float32 (CHANNELS, n_samples).
        """
        with self._lock:
            self._ensure()
            r = self.receiver
            if r is None:
                return None, None
            try:
                fs = r.frame_sync
                fs.capture_video()
                fs.capture_audio(n_samples)
                self._stats(r)
                video = None
                w, h = self.video.get_resolution()
                if want_video and w > 0 and h > 0:
                    arr = self.video.get_array()
                    if (w, h) != (self.width, self.height):
                        arr = resize_bgra(arr.reshape(h, w, 4), self.width, self.height)
                    video = arr.reshape(-1)
                    if not self.keep_alpha and self.fmt.get("alpha"):
                        video = video.copy()
                        video[3::4] = 255           # a source with alpha goes on air full screen, opaque
                audio = fit_audio(pick_pair(self.audio.get_array(), self.audio_pair), n_samples)
                return video, audio
            except Exception:
                return None, None

    def set_tally(self, program, preview):
        """Red / green tally light on the source (cameras and vMix inputs that show NDI tally).
        Sent at once, also to a source that is not being pulled (a live card that is only next)."""
        self.tally = (bool(program), bool(preview))
        if self.tally == self._sent_tally:
            return
        with self._lock:
            r = self.receiver
            if r is not None:
                self._send_tally(r)

    def _send_tally(self, r):
        try:
            r.set_source_tally_program(self.tally[0])
            r.set_source_tally_preview(self.tally[1])
            self._sent_tally = self.tally
        except Exception:
            pass

    def _stats(self, r):
        """Format, frames per second really received and dropped frames, about once a second."""
        if self.tally != self._sent_tally:
            self._send_tally(r)
        now = time.monotonic()
        if now - self._stat_t < 1.0:
            return
        try:
            w, h = self.video.get_resolution()
            fourcc = self.video.get_fourcc()
            name = getattr(fourcc, "name", str(fourcc))
            rate = self.video.get_frame_rate()
            self.fmt = {"width": w, "height": h, "fps": round(float(rate), 3) if rate else None,
                        "fourcc": name, "alpha": name in ("BGRA", "RGBA", "UYVA", "PA16"),
                        "channels": self.audio.num_channels, "sample_rate": self.audio.sample_rate}
            perf = r.get_performance_data()["video"]
            total = perf["frames_total"]
            if self._stat_total is not None and now > self._stat_t:
                self.in_fps = round((total - self._stat_total) / (now - self._stat_t), 1)
            self._stat_total = total
            self.dropped = perf["frames_dropped"]
        except Exception:
            pass
        self._stat_t = now

    def info(self):
        return {"source": self.source_name, "connected": self.connected, "format": dict(self.fmt),
                "in_fps": self.in_fps, "dropped": self.dropped, "tally": list(self.tally),
                "audio_pair": self.audio_pair}

    def close(self):
        with self._lock:
            self._close()


def resize_bgra(img, width, height):
    """Scale with FFmpeg's swscale. Set vMix's output to the same size to skip this."""
    import av
    frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(img), format="bgra")
    return frame.reformat(width=width, height=height).to_ndarray()


def pick_pair(a, pair):
    """Channels 2*pair+1 and 2*pair+2 of a multi-channel source (e.g. 3-4 of an 8 channel feed)."""
    if not pair or a is None or a.ndim != 2 or a.shape[0] <= 2 * pair:
        return a
    return a[2 * pair:2 * pair + 2]


def fit_audio(a, n_samples):
    """Coerce any (channels, samples) array to stereo with exactly n_samples."""
    if a is None or a.ndim != 2 or a.shape[0] == 0:
        return np.zeros((CHANNELS, n_samples), np.float32)
    if a.shape[0] == 1:
        a = np.vstack([a, a])
    elif a.shape[0] > CHANNELS:
        a = a[:CHANNELS]
    if a.shape[1] < n_samples:
        a = np.pad(a, ((0, 0), (0, n_samples - a.shape[1])))
    elif a.shape[1] > n_samples:
        a = a[:, :n_samples]
    return np.ascontiguousarray(a, dtype=np.float32)


class NDIOutput:
    """The playout's program output as an NDI source. The engine's loop keeps the frame rate itself."""

    def __init__(self, name, width, height, fps: Fraction, max_samples=None, paced=False):
        self.name = name
        self._pacer = None
        self.sender = Sender(name, clock_video=False, clock_audio=False)
        vf = VideoSendFrame()
        vf.set_resolution(width, height)
        vf.set_frame_rate(fps)
        vf.set_fourcc(FourCC.BGRA)
        self.sender.set_video_frame(vf)
        af = AudioSendFrame()
        af.sample_rate = SAMPLE_RATE
        af.num_channels = CHANNELS
        af.set_max_num_samples(max_samples or int(SAMPLE_RATE / float(fps)) + 8)
        self.sender.set_audio_frame(af)
        self.sender.open()
        if paced:
            self._pacer = _Pacer(self.sender, fps)

    def send(self, video, audio):
        if self._pacer is not None:
            self._pacer.push(video, audio)
        else:
            self.sender.write_video_and_audio(video, audio)

    def stats(self):
        """Pacer counts (None when not paced): repeats = frames sent again because none was ready."""
        p = self._pacer
        return None if p is None else {"repeats": p.repeats, "dropped": p.dropped, "buffered": len(p.q)}

    def connections(self):
        """How many receivers (vMix, OBS, a monitor) are watching this output."""
        try:
            return int(self.sender.get_num_connections(0))
        except Exception:
            return None

    def tally(self):
        """(program, preview): a receiver has this output on air / on preview."""
        try:
            self.sender.update_tally(0)
            return bool(self.sender.program_tally), bool(self.sender.preview_tally)
        except Exception:
            return False, False

    def close(self):
        if self._pacer is not None:
            self._pacer.stop()
        self.sender.close()


class _Pacer:
    """Sends the program to NDI on an exact frame clock, like a hardware output.

    The engine makes frames on its own clock, and a slow frame is made up by the next ones, so frames come
    out of it unevenly. Receivers (vMix, OBS) then see the frame rate move. Here frames wait in a short
    buffer (BUFFER frames) and one leaves every frame time, timed to well under a millisecond. When none
    is ready the last picture goes out again with silence, and one frame is dropped later to stay in time.
    """
    BUFFER = 3
    MAX = 8

    def __init__(self, sender, fps):
        self.sender = sender
        self.period = 1.0 / float(fps)
        self.q = collections.deque()
        self.cv = threading.Condition()
        self.repeats = self.dropped = 0
        self._debt = 0
        self._last = None
        self._running = True
        _fine_timer()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ndi pacer")
        self._thread.start()

    def push(self, video, audio):
        # the engine may reuse its frame buffers, so keep a copy of the picture
        item = (np.array(video, copy=True), audio)
        with self.cv:
            self.q.append(item)
            while len(self.q) > self.MAX:          # the engine ran far ahead (after a stall): keep in time
                self.q.popleft()
                self.dropped += 1
            self.cv.notify()

    def stop(self):
        self._running = False
        with self.cv:
            self.cv.notify()
        self._thread.join(timeout=1)

    def _run(self):
        while self._running:
            with self.cv:                          # start (and restart after a long gap) with a full buffer
                while self._running and len(self.q) < self.BUFFER:
                    self.cv.wait(0.5)
            due = time.perf_counter()
            idle = 0
            while self._running and idle < 25:
                _wait_until(due)
                due += self.period
                with self.cv:
                    item = self.q.popleft() if self.q else None
                    if item is not None and self._debt and len(self.q) >= self.BUFFER:
                        item = self.q.popleft()    # make up for a repeated frame
                        self._debt -= 1
                        self.dropped += 1
                if item is None:
                    if self._last is None:
                        idle += 1
                        continue
                    idle += 1
                    self.repeats += 1
                    self._debt += 1
                    item = (self._last[0], np.zeros_like(self._last[1]))
                else:
                    idle = 0
                    self._last = item
                try:
                    self.sender.write_video_and_audio(item[0], item[1])
                except Exception:
                    pass
                if time.perf_counter() - due > 1.0:  # the PC stalled for over a second: start over
                    break
            self._debt = 0


def _wait_until(t):
    """Sleep until perf_counter() reaches t; the last 2 ms spin, so the frame leaves on time."""
    while True:
        left = t - time.perf_counter()
        if left <= 0:
            return
        if left > 0.002:
            time.sleep(left - 0.0015)
        else:
            time.sleep(0)


_timer_set = False


def _fine_timer():
    """Ask Windows for a 1 ms timer, so sleeps wake on time (no effect elsewhere)."""
    global _timer_set
    if _timer_set:
        return
    _timer_set = True
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
