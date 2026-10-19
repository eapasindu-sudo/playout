"""Extra NDI outputs next to the program: CLEAN feed and PREVIEW.

    <name> - Clean     the program before CGs, squeeze, comments and stingers (with program sound),
                       for an archive recorder or a second channel
    <name> - Preview   what goes on air next: the next live card's source as it is now, or the first
                       picture of the next clip (no sound), for a studio / director's monitor

Each one sends from its own thread, so the NDI encoding never holds up the program output.
The output loop only hands over its latest picture.
"""
import threading
import time

import numpy as np

from framequeue import FrameQueue
from ndi_io import CHANNELS, SAMPLE_RATE, NDIOutput


class ExtraOutput:
    def __init__(self, name, width, height, fps, sound=True):
        self.name = name
        self.sound = sound
        n = int(SAMPLE_RATE / float(fps)) + 8
        # a picture that was skipped hands its sound on to the next one: room for 10 frames of it
        self._max = n * 10
        self.out = NDIOutput(name, width, height, fps, max_samples=self._max)
        self._q = FrameQueue(2)
        self._silence = np.zeros((CHANNELS, n), np.float32)
        self.sent = 0
        self.error = ""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"ndi {name}")
        self._thread.start()

    def push(self, video, audio):
        """From the output loop: never waits."""
        self._q.put(video, audio if self.sound else self._silence[:, :audio.shape[1]])

    def _run(self):
        while self._running:
            try:
                video, audio, _frames = self._q.get(timeout=0.5)
            except Exception:
                continue
            try:
                if audio.shape[1] > self._max:          # the PC was stuck for a while: keep the latest sound
                    audio = audio[:, -self._max:]
                self.out.send(video, np.ascontiguousarray(audio))
                self.sent += 1
                self.error = ""
            except Exception as e:
                self.error = str(e)[:200]
                time.sleep(0.05)

    def info(self):
        return {"name": self.name, "connections": self.out.connections(), "error": self.error}

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
