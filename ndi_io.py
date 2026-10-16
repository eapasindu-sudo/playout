"""NDI input (vMix program feed) and NDI output, built on cyndilib."""
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
                video = None
                w, h = self.video.get_resolution()
                if want_video and w > 0 and h > 0:
                    arr = self.video.get_array()
                    if (w, h) != (self.width, self.height):
                        arr = resize_bgra(arr.reshape(h, w, 4), self.width, self.height)
                    video = arr.reshape(-1)
                audio = fit_audio(self.audio.get_array(), n_samples)
                return video, audio
            except Exception:
                return None, None

    def close(self):
        with self._lock:
            self._close()


def resize_bgra(img, width, height):
    """Scale with FFmpeg's swscale. Set vMix's output to the same size to skip this."""
    import av
    frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(img), format="bgra")
    return frame.reformat(width=width, height=height).to_ndarray()


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

    def __init__(self, name, width, height, fps: Fraction):
        self.sender = Sender(name, clock_video=False, clock_audio=False)
        vf = VideoSendFrame()
        vf.set_resolution(width, height)
        vf.set_frame_rate(fps)
        vf.set_fourcc(FourCC.BGRA)
        self.sender.set_video_frame(vf)
        af = AudioSendFrame()
        af.sample_rate = SAMPLE_RATE
        af.num_channels = CHANNELS
        af.set_max_num_samples(int(SAMPLE_RATE / float(fps)) + 8)
        self.sender.set_audio_frame(af)
        self.sender.open()

    def send(self, video, audio):
        self.sender.write_video_and_audio(video, audio)

    def close(self):
        self.sender.close()
