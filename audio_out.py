"""Plays the program sound on a sound card of this PC (Windows default output unless another is picked).

The engine pushes every program audio block; the sound card pulls from a small buffer on its own
clock. The buffer is kept short (about 60-200 ms): when the card runs faster we pad silence, when it
runs slower the oldest audio is dropped, so the sound never drifts away from the picture.
"""
import collections
import threading

import numpy as np

from ndi_io import CHANNELS, SAMPLE_RATE

try:
    import sounddevice as sd
    _ERR = ""
except Exception as e:      # PortAudio missing (Linux without libportaudio2)
    sd = None
    _ERR = str(e)


class AudioOut:
    MAX = SAMPLE_RATE // 5         # 200 ms
    TARGET = SAMPLE_RATE * 6 // 100  # trim back to 60 ms

    def __init__(self):
        self.enabled = False
        self.device = ""            # "" = Windows default output
        self.volume = 1.0
        self.error = _ERR
        self._stream = None
        self._buf = collections.deque()
        self._len = 0
        self._lock = threading.Lock()

    # ---- settings ------------------------------------------------------
    def devices(self):
        """Output devices of the default host API (the names Windows shows in Sound settings)."""
        if sd is None:
            return []
        try:
            api = sd.default.hostapi if hasattr(sd.default, "hostapi") else 0
            out = []
            for d in sd.query_devices():
                if d["max_output_channels"] > 0 and d["hostapi"] == api and d["name"] not in out:
                    out.append(d["name"])
            return out
        except Exception:
            return []

    def configure(self, enabled=None, device=None, volume=None):
        restart = False
        if device is not None and device != self.device:
            self.device = device
            restart = True
        if volume is not None:
            self.volume = max(0.0, min(2.0, float(volume)))
        if enabled is not None and bool(enabled) != self.enabled:
            self.enabled = bool(enabled)
            restart = True
        if restart:
            self._close()
            if self.enabled:
                self._open()
        return self.info()

    def info(self):
        return {"enabled": self.enabled, "device": self.device, "volume": self.volume,
                "running": self._stream is not None, "error": self.error, "devices": self.devices()}

    # ---- stream --------------------------------------------------------
    def _open(self):
        if sd is None:
            self.error = "sound output not available: " + _ERR
            return
        try:
            dev = self._device_index(self.device)
            self._stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32",
                                           device=dev, latency="low", callback=self._callback)
            self._stream.start()
            self.error = ""
        except Exception as e:
            self._stream = None
            self.error = str(e)

    def _device_index(self, name):
        if not name:
            return None
        for i, d in enumerate(sd.query_devices()):
            if d["name"] == name and d["max_output_channels"] > 0:
                return i
        return None     # unplugged: fall back to the default output

    def _close(self):
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        with self._lock:
            self._buf.clear()
            self._len = 0

    def close(self):
        self.enabled = False
        self._close()

    def push(self, audio):
        """audio: (channels, samples) float32 program sound for one frame."""
        if self._stream is None or audio is None:
            return
        block = np.ascontiguousarray(audio.T, dtype=np.float32)
        if self.volume != 1.0:
            block = block * np.float32(self.volume)
        with self._lock:
            self._buf.append(block)
            self._len += len(block)
            if self._len > self.MAX:
                drop = self._len - self.TARGET
                while drop > 0 and self._buf:
                    first = self._buf[0]
                    if len(first) <= drop:
                        self._buf.popleft()
                        self._len -= len(first)
                        drop -= len(first)
                    else:
                        self._buf[0] = first[drop:]
                        self._len -= drop
                        drop = 0

    def _callback(self, out, frames, _time, _status):
        got = 0
        with self._lock:
            while got < frames and self._buf:
                chunk = self._buf[0]
                take = min(frames - got, len(chunk))
                out[got:got + take] = chunk[:take]
                if take == len(chunk):
                    self._buf.popleft()
                else:
                    self._buf[0] = chunk[take:]
                self._len -= take
                got += take
        if got < frames:
            out[got:] = 0
        np.clip(out, -1.0, 1.0, out=out)
