"""Extra NDI outputs next to the program: CLEAN feed, PREVIEW and LED.

    <name> - Clean     the program before CGs, squeeze, comments and stingers (with program sound),
                       for an archive recorder or a second channel
    <name> - Preview   what goes on air next: the next live card's source as it is now, or the first
                       picture of the next clip (no sound), for a studio / director's monitor
    <name> - LED       the program with its CGs but without the score overlay (with program sound), for
                       Endora LED Display on a stadium screen that shows the scoreboard beside it

Each one sends from its own thread, so the NDI encoding never holds up the program output.
The output loop only hands over its latest picture.
"""
import logging
import threading
import time

import numpy as np

from framequeue import FrameQueue
from ndi_io import CHANNELS, SAMPLE_RATE, NDIOutput, Uyvy, bgra_to_uyvy, to_bgra


class ExtraOutput:
    # converting on the CPU here costs more than the NDI library's own BGRA path: after this long without
    # UYVY from the GPU engine, the output goes back to sending BGRA
    CPU_SECS = 30

    def __init__(self, name, width, height, fps, sound=True, compose=None, uyvy=False):
        self.name = name
        self.compose = compose          # LED: (program, layers) -> picture, mixed on this thread
        self.sound = sound
        self.width, self.height, self.fps = width, height, fps
        n = int(SAMPLE_RATE / float(fps)) + 8
        # a picture that was skipped hands its sound on to the next one: room for 10 frames of it
        self._max = n * 10
        # UYVY when the GPU engine makes the picture that way (half the bytes, no colour convert on the CPU,
        # and NDI compresses it without an alpha channel)
        self.out = self._new(uyvy)
        self._q = FrameQueue(2)
        self._silence = np.zeros((CHANNELS, n), np.float32)
        self.sent = 0
        self.error = ""
        self._running = True
        self._cpu = 0                   # frames in a row converted here
        self._conv = (None, None)       # (BGRA picture, its UYVY): a still (Preview) is converted once
        self.watched, self._seen_t = True, 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"ndi {name}")
        self._thread.start()

    def _new(self, uyvy):
        return NDIOutput(self.name, self.width, self.height, self.fps, max_samples=self._max, paced=True,
                         uyvy=uyvy)   # same steady clock as the program

    @property
    def uyvy(self):
        return self.out.uyvy

    def push(self, video, audio, uyvy=None):
        """From the output loop: never waits. uyvy: the same picture as UYVY already (GPU engine)."""
        if uyvy is None and isinstance(video, Uyvy):
            w, h = video.w, video.h
            video = video.copy().view(Uyvy)     # the receiver's buffer is used again for the next picture
            video.w, video.h = w, h
        self._q.put((video, uyvy), audio if self.sound else self._silence[:, :audio.shape[1]])

    def _to_uyvy(self, video):
        """(picture, cpu): video as flat UYVY of the output size; cpu = it had to be converted here."""
        if isinstance(video, Uyvy) and (video.w, video.h) == (self.width, self.height):
            return video, False                  # the live input's own picture: nothing to do
        if self._conv[0] is video:
            return self._conv[1], False
        bgra = to_bgra(video, self.width, self.height)
        uv = bgra_to_uyvy(bgra, self.width, self.height)
        self._conv = (video, uv)
        return uv, True

    def _back_to_bgra(self):
        logging.warning("NDI %s: no UYVY from the GPU engine, sending BGRA again", self.name)
        old, self.out = self.out, self._new(False)
        threading.Timer(0.5, old.close).start()

    def _run(self):
        while self._running:
            try:
                (video, uv), audio, _frames = self._q.get(timeout=0.5)
            except Exception:
                continue
            now = time.monotonic()
            if now - self._seen_t > 1.0:               # who is watching, checked about once a second
                self._seen_t = now
                c = self.out.connections()
                self.watched = c is None or c > 0
            if not self.watched:
                continue        # nobody is receiving this output: skip the mixing and sending (CPU)
            try:
                out = self.out
                if uv is not None and not out.uyvy:      # went back to BGRA meanwhile
                    uv = None
                    if isinstance(video, tuple) and video[0] is None:
                        continue
                if uv is None:
                    if self.compose is not None:
                        video = self.compose(video)
                    if out.uyvy:
                        uv, cpu = self._to_uyvy(video)
                        self._cpu = self._cpu + 1 if cpu else 0
                        if self._cpu > self.CPU_SECS * float(self.fps):
                            self._back_to_bgra()
                    else:
                        video = to_bgra(video, self.width, self.height)
                else:
                    self._cpu = 0
                if audio.shape[1] > self._max:          # the PC was stuck for a while: keep the latest sound
                    audio = audio[:, -self._max:]
                if out.uyvy:
                    out.send(None, np.ascontiguousarray(audio), uyvy=uv, owned=True)
                else:
                    out.send(video, np.ascontiguousarray(audio))
                self.sent += 1
                self.error = ""
            except Exception as e:
                self.error = str(e)[:200]
                time.sleep(0.05)

    def info(self):
        return {"name": self.name, "connections": self.out.connections(), "error": self.error,
                "fourcc": "UYVY" if self.out.uyvy else "BGRA"}

    def close(self):
        self._running = False
        self._thread.join(timeout=2)
        self.out.close()


def still_frame(path, start, width, height):
    """First picture of a clip (at its trim in point) as a flat BGRA frame of the output size."""
    import av
    try:
        with av.open(path) as c:
            vs = c.streams.video[0]
            base = float(vs.start_time * vs.time_base) if vs.start_time is not None else 0.0
            if start > 0:
                c.seek(int((base + start) / vs.time_base), stream=vs, backward=True)
            for fr in c.decode(vs):
                if fr.time is None or fr.time >= base + start - 0.02:
                    return fr.reformat(width=width, height=height, format="bgra").to_ndarray().reshape(-1)
    except Exception:
        return None
    return None
