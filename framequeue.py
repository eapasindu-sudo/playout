"""Program frames on their way to an encoder thread (stream, recording).

When the encoder falls behind, a picture is skipped but its sound never is: the sound of skipped frames
rides along with the next frame that gets in, and the encoder moves the picture clock on by that many
frames. So a busy PC gives a held picture for a moment, never a click or a gap in the sound.
"""
import queue

import numpy as np

MAX_SPILL = 48000 * 10          # never hold more than 10 s of sound for a stuck encoder


class FrameQueue:
    def __init__(self, size):
        self._q = queue.Queue(maxsize=size)
        self._spill = []
        self._spill_len = 0

    def put(self, video, audio):
        """From the output loop. False when this frame's picture had to be skipped."""
        frames = len(self._spill) + 1
        a = np.concatenate(self._spill + [audio], axis=1) if self._spill else audio
        try:
            self._q.put_nowait((video, a, frames))
        except queue.Full:
            self._spill.append(audio)
            self._spill_len += audio.shape[1]
            while self._spill_len > MAX_SPILL and len(self._spill) > 1:
                self._spill_len -= self._spill.pop(0).shape[1]
            return False
        self._spill, self._spill_len = [], 0
        return True

    def get(self, timeout):
        """-> (video, audio, frames): frames > 1 when pictures were skipped before this one."""
        return self._q.get(timeout=timeout)

    def empty(self):
        return self._q.empty()
