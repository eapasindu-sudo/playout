"""NDI input (vMix program feed) and NDI output, built on cyndilib."""
import logging
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
        self.uyvy = False                # True: take pictures without alpha as UYVY (grab(raw=True) hands them on)
        # what the source sends (NDI Sources panel / health): filled while frames come in
        self.fmt = {}
        self.in_fps = None
        self.dropped = 0
        self._stat_t = 0.0
        self._stat_total = None
        self._abuf = None                # live sound waiting to go out (CHANNELS, samples)
        self._prime = self.PRIME
        self._primed = False
        self._lvl = None
        self.underruns = 0
        self._under_t = 0.0

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
        r = Receiver(color_format=RecvColorFormat.UYVY_BGRA if self.uyvy else RecvColorFormat.BGRX_BGRA,
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

    def grab(self, n_samples, want_video=True, raw=False):
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
                audio = self._take_audio(fs, n_samples)
                self._stats(r)
                video = None
                w, h = self.video.get_resolution()
                if want_video and w > 0 and h > 0:
                    arr = self.video.get_array()
                    if self.uyvy and self.video.get_fourcc() == FourCC.UYVY:
                        stride = self.video.get_line_stride()
                        if stride != w * 2:
                            arr = np.ascontiguousarray(arr[:stride * h].reshape(h, stride)[:, :w * 2]).reshape(-1)
                        if raw and w * h * 2 <= self.width * self.height * 4 and not (w & 1):
                            video = arr.view(Uyvy)           # the GPU engine converts it
                            video.w, video.h = w, h
                            return video, audio
                        return uyvy_to_bgra(arr, w, h, self.width, self.height), audio
                    if (w, h) != (self.width, self.height):
                        arr = resize_bgra(arr.reshape(h, w, 4), self.width, self.height)
                    video = arr.reshape(-1)
                    if not self.keep_alpha and self.fmt.get("alpha"):
                        video = video.copy()
                        video[3::4] = 255           # a source with alpha goes on air full screen, opaque
                return video, audio
            except Exception:
                return None, None

    # NDI hands over only the sound that has arrived, and vMix sends it in bursts: taking exactly one frame's
    # worth each frame left 0.1 s gaps every 0.3 s (heard as crackling on the stream). The sound now goes
    # through a small buffer: it starts once PRIME seconds are in, each frame takes exactly its samples, and
    # the buffer grows a little after each time it runs dry, up to PRIME_MAX.
    PRIME = 0.08
    PRIME_MAX = 0.4

    def _take_audio(self, fs, n):
        rate = SAMPLE_RATE
        avail = fs.audio_samples_available()
        if avail > 0:
            fs.capture_audio(avail)
            a = _stereo(pick_pair(self.audio.get_array(), self.audio_pair))
            if a is not None and a.shape[1]:
                self._abuf = a if self._abuf is None else np.concatenate([self._abuf, a], axis=1)
        buf = self._abuf
        have = 0 if buf is None else buf.shape[1]
        prime = int(self._prime * rate)
        if not self._primed:
            if have < n + prime:
                return np.zeros((CHANNELS, n), np.float32)
            self._primed = True
        if have > n + prime + int(0.25 * rate):        # the source runs ahead of our clock: catch up
            buf = buf[:, have - (n + prime):]
            have = buf.shape[1]
        if have < n:                                    # ran dry: wait for a bigger buffer
            self.underruns += 1
            self._prime = min(self.PRIME_MAX, self._prime + 0.04)
            self._primed = False
            now = time.monotonic()
            if now - self._under_t > 60:
                self._under_t = now
                logging.info("live input %s: sound ran dry (%d times), buffer now %.0f ms",
                             self.source_name, self.underruns, self._prime * 1000)
            out = np.zeros((CHANNELS, n), np.float32)
            if have:
                out[:, :have] = buf
            self._abuf = None
            return out
        # keep the buffer near its level by playing it 0.5 % slower or faster (not heard), so a source clock
        # a little slower or faster than ours never runs it dry or makes it grow
        self._lvl = have if self._lvl is None else 0.97 * self._lvl + 0.03 * have
        target = n + prime + int(0.04 * rate)
        m = n
        if self._lvl < target * 0.85:
            m = n - n // 200
        elif self._lvl > target * 1.25:
            m = n + n // 200
        m = min(m, have)
        if m == n:
            out = buf[:, :n]
        else:
            x = np.linspace(0, m - 1, n)
            out = np.vstack([np.interp(x, np.arange(m), buf[c, :m]) for c in range(buf.shape[0])])
        self._abuf = buf[:, m:]
        return np.ascontiguousarray(out, dtype=np.float32)

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


class Uyvy(np.ndarray):
    """A live picture still in the source's own UYVY 4:2:2 (flat, w * h * 2 bytes). The GPU engine converts
    and scales it as it mixes (half the bytes to hand over, no colour convert on the CPU); bgra() does it
    here, for anything else that needs the picture."""
    uyvy = True
    w = h = 0

    def bgra(self, width, height):
        return uyvy_to_bgra(self, self.w, self.h, width, height)


def to_bgra(v, width, height):
    """v as flat BGRA of width x height (a Uyvy picture converted, anything else as it is)."""
    return v.bgra(width, height) if isinstance(v, Uyvy) else v


def bgra_to_uyvy(a, w, h):
    """Flat BGRA w x h as flat UYVY (BT.709 video levels), when the GPU engine did not convert it."""
    import av
    f = av.VideoFrame.from_ndarray(np.asarray(a, np.uint8).reshape(h, w, 4), format="bgra")
    o = f.reformat(format="uyvy422", dst_colorspace="itu709")
    p = o.planes[0]
    return np.ascontiguousarray(np.frombuffer(p, np.uint8).reshape(h, p.line_size)[:, :w * 2]).reshape(-1)


def uyvy_to_bgra(a, w, h, width, height):
    import av
    f = av.VideoFrame(w, h, "uyvy422")
    p = f.planes[0]
    np.frombuffer(p, np.uint8).reshape(h, p.line_size)[:, :w * 2] = np.asarray(a, np.uint8).reshape(h, w * 2)
    out = f.reformat(width=width, height=height, format="bgra", src_colorspace="itu709" if h >= 720 else "itu601")
    return out.to_ndarray().reshape(-1)


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


def _stereo(a):
    if a is None or a.ndim != 2 or a.shape[0] == 0:
        return None
    if a.shape[0] == 1:
        a = np.vstack([a, a])
    elif a.shape[0] > CHANNELS:
        a = a[:CHANNELS]
    return np.array(a, dtype=np.float32)    # a copy: the NDI buffer is reused on the next capture


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

    def __init__(self, name, width, height, fps: Fraction, max_samples=None, paced=False, uyvy=False):
        self.name = name
        self.width, self.height = width, height
        self.uyvy = bool(uyvy) and not (width & 1)   # sent as UYVY (the GPU engine converts the program)
        self._pacer = None
        self.sender = Sender(name, clock_video=False, clock_audio=False)
        vf = VideoSendFrame()
        vf.set_resolution(width, height)
        vf.set_frame_rate(fps)
        vf.set_fourcc(FourCC.UYVY if self.uyvy else FourCC.BGRA)
        self.sender.set_video_frame(vf)
        af = AudioSendFrame()
        af.sample_rate = SAMPLE_RATE
        af.num_channels = CHANNELS
        # Every sound frame sent is exactly BLOCK samples. cyndilib lays a shorter frame out packed but tells NDI
        # the channels are max_num_samples apart, so with a larger maximum (1920 + 8 before) the right channel
        # came out 8 samples off with 8 stale ones each frame: a click 25 times a second (crackling in vMix).
        self.block = int(round(SAMPLE_RATE / float(fps)))
        af.set_max_num_samples(self.block)
        self.sender.set_audio_frame(af)
        self.sender.open()
        self.out = _BlockSender(self.sender, self.block)
        logging.info("NDI output on: %s (%dx%d)", name, width, height)
        if paced:
            self._pacer = _Pacer(self.out, fps)

    def send(self, video, audio, uyvy=None):
        """uyvy: the same picture as UYVY already (from the GPU engine), used when this output sends UYVY."""
        audio = np.ascontiguousarray(audio, dtype=np.float32)   # NDI takes float32 only (mixing can give float64)
        if self.uyvy:
            video = uyvy if uyvy is not None else bgra_to_uyvy(video, self.width, self.height)
        if self._pacer is not None:
            self._pacer.push(video, audio)
        else:
            self.out.write_video_and_audio(video, audio)

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


class _BlockSender:
    """Sends sound in frames of exactly `block` samples (the rest waits for the next frame)."""

    def __init__(self, sender, block):
        self.sender = sender
        self.block = block
        self._carry = None

    def _blocks(self, audio):
        a = audio if self._carry is None else np.concatenate([self._carry, audio], axis=1)
        n = a.shape[1] // self.block
        out = [np.ascontiguousarray(a[:, i * self.block:(i + 1) * self.block], dtype=np.float32) for i in range(n)]
        rest = a[:, n * self.block:]
        self._carry = rest.copy() if rest.shape[1] else None
        return out

    def write_audio(self, audio):
        for b in self._blocks(audio):
            self.sender.write_audio(b)

    def write_video_async(self, video):
        self.sender.write_video_async(video)

    def write_video_and_audio(self, video, audio):
        blocks = self._blocks(audio)
        if len(blocks) == 1:
            self.sender.write_video_and_audio(video, blocks[0])
            return
        for b in blocks:
            self.sender.write_audio(b)
        self.sender.write_video_async(video)


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
                skipped = None
                with self.cv:
                    item = self.q.popleft() if self.q else None
                    if item is not None and self._debt and len(self.q) >= self.BUFFER:
                        skipped = item             # make up for a repeated frame: its picture is skipped,
                        item = self.q.popleft()    # its sound still goes out (no gap, no click)
                        self._debt -= 1
                        self.dropped += 1
                try:
                    if item is None:
                        idle += 1
                        if self._last is None:
                            continue
                        self.repeats += 1
                        self._debt += 1
                        # the picture again, and no sound: the sound of the late frame follows it unbroken
                        # (silence put in here was heard as crackling in vMix)
                        self.sender.write_video_async(self._last[0])
                    else:
                        idle = 0
                        self._last = item
                        if skipped is not None:
                            self.sender.write_audio(skipped[1])
                        self.sender.write_video_and_audio(item[0], item[1])
                except Exception as e:
                    now = time.monotonic()
                    if now - getattr(self, "_err_t", 0.0) > 60:        # at most one line a minute
                        self._err_t = now
                        logging.warning("NDI send failed: %s", e)
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
