"""Which graphics card video encoder this PC has, so streaming and recording run on the GPU
instead of the CPU.

"auto" = the first GPU encoder that really opens on this PC: NVIDIA (NVENC), Intel (Quick Sync),
AMD (AMF); the CPU (x264) only when the PC has none. NVENC / AMF also take the BGRA program
picture as it is, so the colour conversion runs on the graphics card too.
"""
import threading
from fractions import Fraction

import av
import numpy as np

GPU_ENCODERS = ("h264_nvenc", "h264_qsv", "h264_amf")
ENCODERS = ("auto", "libx264") + GPU_ENCODERS
NAMES = {"h264_nvenc": "NVIDIA", "h264_qsv": "Intel", "h264_amf": "AMD", "libx264": "CPU (x264)"}

_lock = threading.Lock()
_tested = {}            # encoder -> None (does not work) or the input pixel format it takes


def _try(enc, pix):
    ctx = av.CodecContext.create(enc, "w")
    ctx.width, ctx.height, ctx.pix_fmt = 640, 360, pix
    ctx.time_base = Fraction(1, 25)
    ctx.framerate = Fraction(25, 1)
    ctx.bit_rate = 1_000_000
    ctx.open()
    f = av.VideoFrame.from_ndarray(np.zeros((360, 640, 4), np.uint8), format="bgra")
    if pix != "bgra":
        f = f.reformat(format=pix)
    f.pts = 0
    ctx.encode(f)
    ctx.encode(None)


ERRORS = {}     # GPU encoder -> why it did not open on this PC (health page)


def input_format(enc):
    """The pixel format to hand this encoder (bgra = no CPU colour conversion), or None if it
    does not work on this PC."""
    if enc == "libx264":
        return "yuv420p"
    with _lock:
        if enc not in _tested:
            _tested[enc] = None
            for pix in (("bgra", "nv12") if enc != "h264_qsv" else ("nv12",)):
                try:
                    _try(enc, pix)
                    _tested[enc] = pix
                    ERRORS.pop(enc, None)
                    break
                except Exception as e:
                    ERRORS[enc] = str(e)[:200] or type(e).__name__
                    continue
        return _tested[enc]


def best():
    for enc in GPU_ENCODERS:
        if input_format(enc):
            return enc
    return "libx264"


def resolve(enc):
    """'auto' -> the best encoder on this PC; a GPU encoder that does not work -> CPU."""
    if enc in GPU_ENCODERS and input_format(enc):
        return enc
    if enc == "libx264":
        return enc
    return best()


def hw_options(enc, kbps, gop):
    """Low-delay constant-bitrate settings for each GPU encoder."""
    k = f"{kbps}k"
    if enc == "h264_nvenc":
        return {"preset": "p4", "tune": "ll", "rc": "cbr", "maxrate": k, "bufsize": f"{kbps * 2}k",
                "g": str(gop), "bf": "0", "forced-idr": "1"}
    if enc == "h264_qsv":
        return {"preset": "veryfast", "maxrate": k, "bufsize": f"{kbps * 2}k", "g": str(gop), "bf": "0",
                "look_ahead": "0"}
    if enc == "h264_amf":
        return {"usage": "lowlatency", "rc": "cbr", "quality": "speed", "maxrate": k,
                "bufsize": f"{kbps * 2}k", "g": str(gop), "bf": "0"}
    return {"g": str(gop), "bf": "0"}


def info():
    return {"best": best(), "name": NAMES.get(best(), best()),
            "available": [e for e in GPU_ENCODERS if input_format(e)]}
