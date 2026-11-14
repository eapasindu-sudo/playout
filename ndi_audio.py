"""Sound of an NDI source, pulled the way the NDI SDK means it: a steady number of samples every frame.

The NDI frame-sync (Processing.NDI.FrameSync.h) returns exactly the samples asked for, at once, and resamples
the source so its clock follows the rate it is called at ("You should call this at the rate that you want audio
and it will automatically adapt the incoming audio signal to match the rate at which you are calling").
cyndilib's FrameSync.capture_audio() only hands over what is already queued, so Playout could not ask for a
frame's worth; pulling the queue instead left the program silent most of the time with vMix (bursts only).

This opens a second, sound-only receiver on the same source (NDIlib_recv_bandwidth_audio_only) with its own
frame-sync, through the NDI library cyndilib already loaded, and calls NDIlib_framesync_capture_audio_v2 with
48000 Hz and one frame's samples. Structures and calls are as declared in the NDI SDK headers shipped with
cyndilib (cyndilib/wrapper/include). If the library cannot be found, LiveInput keeps its own buffer.
"""
import ctypes
import glob
import logging
import os
import sys
import time

import numpy as np

SAMPLE_RATE = 48000
_BANDWIDTH_AUDIO_ONLY = 10          # NDIlib_recv_bandwidth_audio_only
_COLOR_FASTEST = 100                # NDIlib_recv_color_format_fastest (no video is received)
_FOURCC_FLTP = ord("F") | ord("L") << 8 | ord("T") << 16 | ord("p") << 24


class _Source(ctypes.Structure):            # NDIlib_source_t
    _fields_ = [("p_ndi_name", ctypes.c_char_p), ("p_url_address", ctypes.c_char_p)]


class _RecvCreate(ctypes.Structure):        # NDIlib_recv_create_v3_t
    _fields_ = [("source_to_connect_to", _Source), ("color_format", ctypes.c_int), ("bandwidth", ctypes.c_int),
                ("allow_video_fields", ctypes.c_bool), ("p_ndi_recv_name", ctypes.c_char_p)]


class _AudioFrame(ctypes.Structure):        # NDIlib_audio_frame_v3_t
    _fields_ = [("sample_rate", ctypes.c_int), ("no_channels", ctypes.c_int), ("no_samples", ctypes.c_int),
                ("timecode", ctypes.c_int64), ("FourCC", ctypes.c_int), ("p_data", ctypes.c_void_p),
                ("channel_stride_in_bytes", ctypes.c_int), ("p_metadata", ctypes.c_char_p),
                ("timestamp", ctypes.c_int64)]


_lib = None
_lib_error = None


def _candidates():
    try:
        import cyndilib
        root = os.path.dirname(cyndilib.__file__)
    except Exception:
        root = None
    pats = []
    if sys.platform == "win32":
        names = ["Processing.NDI.Lib.x64.dll"]
    elif sys.platform == "darwin":
        names = ["libndi.dylib", "libndi*.dylib"]
    else:
        names = ["libndi.so*"]
    for base in filter(None, [root, getattr(sys, "_MEIPASS", None),
                              os.environ.get("NDI_RUNTIME_DIR_V6"), os.environ.get("NDI_RUNTIME_DIR_V5"),
                              "/Library/NDI SDK for Apple/lib/macOS", "/usr/local/lib"]):
        for n in names:
            pats += [os.path.join(base, n), os.path.join(base, "**", n)]
    seen = []
    for p in pats:
        for f in glob.glob(p, recursive=True):
            if f not in seen:
                seen.append(f)
    if sys.platform == "win32":
        seen.append("Processing.NDI.Lib.x64.dll")      # already loaded by cyndilib: LoadLibrary finds it by name
    return seen


def lib():
    """The NDI library (the one cyndilib uses), or None."""
    global _lib, _lib_error
    if _lib is not None or _lib_error is not None:
        return _lib
    for path in _candidates():
        try:
            L = ctypes.CDLL(path)
            L.NDIlib_initialize.restype = ctypes.c_bool
            L.NDIlib_recv_create_v3.restype = ctypes.c_void_p
            L.NDIlib_recv_create_v3.argtypes = [ctypes.POINTER(_RecvCreate)]
            L.NDIlib_recv_destroy.argtypes = [ctypes.c_void_p]
            L.NDIlib_recv_destroy.restype = None
            L.NDIlib_recv_get_no_connections.argtypes = [ctypes.c_void_p]
            L.NDIlib_recv_get_no_connections.restype = ctypes.c_int
            L.NDIlib_framesync_create.restype = ctypes.c_void_p
            L.NDIlib_framesync_create.argtypes = [ctypes.c_void_p]
            L.NDIlib_framesync_destroy.argtypes = [ctypes.c_void_p]
            L.NDIlib_framesync_destroy.restype = None
            L.NDIlib_framesync_capture_audio_v2.argtypes = [ctypes.c_void_p, ctypes.POINTER(_AudioFrame),
                                                             ctypes.c_int, ctypes.c_int, ctypes.c_int]
            L.NDIlib_framesync_capture_audio_v2.restype = None
            L.NDIlib_framesync_free_audio_v2.argtypes = [ctypes.c_void_p, ctypes.POINTER(_AudioFrame)]
            L.NDIlib_framesync_free_audio_v2.restype = None
            L.NDIlib_framesync_audio_queue_depth.argtypes = [ctypes.c_void_p]
            L.NDIlib_framesync_audio_queue_depth.restype = ctypes.c_int
            if not L.NDIlib_initialize():
                raise OSError("NDIlib_initialize failed")
            _lib = L
            logging.info("NDI sound: frame-sync pull through %s", path)
            return _lib
        except (OSError, AttributeError) as e:
            _lib_error = f"{path}: {e}"
    _lib_error = _lib_error or "NDI library not found"
    logging.warning("NDI sound: %s (live sound uses Playout's own buffer)", _lib_error)
    return None


class FrameSyncAudio:
    """A sound-only NDI receiver for one source; pull(n) gives (2, n) float32 at 48 kHz every frame."""

    def __init__(self, source_name, pair=0, url=None):
        L = lib()
        if L is None:
            raise OSError(_lib_error or "NDI library not found")
        self._L = L
        self.source_name = source_name
        self.pair = pair
        self._name = source_name.encode("utf-8")
        self._recv_name = b"Playout Live In (sound)"
        cs = _RecvCreate(_Source(self._name, url.encode() if url else None), _COLOR_FASTEST, _BANDWIDTH_AUDIO_ONLY, True, self._recv_name)
        self._recv = L.NDIlib_recv_create_v3(ctypes.byref(cs))
        if not self._recv:
            raise OSError("NDIlib_recv_create_v3 failed")
        self._fs = L.NDIlib_framesync_create(self._recv)
        if not self._fs:
            L.NDIlib_recv_destroy(self._recv)
            self._recv = None
            raise OSError("NDIlib_framesync_create failed")
        self._frame = _AudioFrame()
        # what the source sends and how the pull goes (NDI Sources panel / health)
        self.src_rate = 0
        self.src_channels = 0
        self.depth = 0
        self.pulls = 0
        self._fmt_t = 0.0
        self._win = []                  # (time, samples, silent samples) of the last seconds
        self.silent_pct = None

    def _format(self):
        """The source's own rate / channels: a capture with all zeros returns them (FrameSync.h)."""
        f = self._frame
        self._L.NDIlib_framesync_capture_audio_v2(self._fs, ctypes.byref(f), 0, 0, 0)
        self.src_rate, self.src_channels = f.sample_rate, f.no_channels
        if f.p_data:
            self._L.NDIlib_framesync_free_audio_v2(self._fs, ctypes.byref(f))
        self.depth = self._L.NDIlib_framesync_audio_queue_depth(self._fs)

    def pull(self, n):
        if self._fs is None:
            return np.zeros((2, n), np.float32)
        now = time.monotonic()
        if now - self._fmt_t > 1.0:
            self._fmt_t = now
            try:
                self._format()
            except Exception:
                pass
        # stereo, or the source's own channels when a pair other than 1-2 is wanted
        ch = 2 if not self.pair else max(2, self.src_channels or 2)
        f = self._frame
        self._L.NDIlib_framesync_capture_audio_v2(self._fs, ctypes.byref(f), SAMPLE_RATE, ch, n)
        try:
            got, nch, stride = f.no_samples, f.no_channels, f.channel_stride_in_bytes
            if not f.p_data or got <= 0 or nch <= 0 or f.FourCC != _FOURCC_FLTP:
                out = np.zeros((2, n), np.float32)
            else:
                step = stride // 4 if stride else got
                buf = (ctypes.c_float * (step * (nch - 1) + got)).from_address(f.p_data)
                a = np.frombuffer(buf, np.float32)
                a = np.stack([a[c * step:c * step + got] for c in range(nch)])
                lo = 2 * self.pair if self.pair and nch > 2 * self.pair else 0
                a = a[lo:lo + 2]
                if a.shape[0] == 1:
                    a = np.vstack([a, a])
                out = np.zeros((2, n), np.float32)
                k = min(n, got)
                out[:, :k] = a[:, :k]
        finally:
            self._L.NDIlib_framesync_free_audio_v2(self._fs, ctypes.byref(f))
        self.pulls += 1
        self._count(now, out)
        return out

    def _count(self, now, out):
        quiet = int(np.count_nonzero(np.max(np.abs(out), axis=0) < 1e-5))
        self._win.append((now, out.shape[1], quiet))
        while self._win and now - self._win[0][0] > 5.0:
            self._win.pop(0)
        tot = sum(w[1] for w in self._win)
        self.silent_pct = round(100.0 * sum(w[2] for w in self._win) / tot, 1) if tot else None

    def info(self):
        return {"mode": "ndi-framesync", "source_rate": self.src_rate, "source_channels": self.src_channels,
                "queue_samples": self.depth, "pulls": self.pulls, "silent_pct_5s": self.silent_pct}

    def close(self):
        L = self._L
        if self._fs:
            L.NDIlib_framesync_destroy(self._fs)
            self._fs = None
        if self._recv:
            L.NDIlib_recv_destroy(self._recv)
            self._recv = None
