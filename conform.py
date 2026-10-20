"""Converter: makes a copy of a clip that is not in the program's own format (another frame rate, size
or a heavy codec such as HEVC / ProRes) at the program's frame rate and size, next to the
original in the media folder ("<name> 25p.mp4"). The operator starts it from the clip library and adds
the copy to the playlist when it is done: a 25p 1080 file plays at a fraction of the CPU / GPU cost.
Nothing is converted on its own and the original is never changed.
"""
import logging
import os
import queue
import subprocess
import sys
import threading

import av

import gpu

STATUS = {}             # clip name -> "waiting" / "converting 42%" / "ready" / "failed: ..." (health page)
_q = queue.Queue()
_queued = set()
_lock = threading.Lock()
_thread = None
_probe = {}


def _info(path):
    """(fps, width, height) of the clip's video, cached by path + mtime."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key not in _probe:
        r = None
        try:
            with av.open(path) as c:
                v = c.streams.video[0]
                rate = v.average_rate or v.guessed_rate
                r = (float(rate) if rate else 0.0, v.codec_context.width, v.codec_context.height,
                     v.codec_context.name)
        except Exception:
            r = None
        _probe[key] = r
    return _probe[key]


def target(path, fps):
    stem = os.path.splitext(path)[0]
    return f"{stem} {int(round(float(fps)))}p.mp4"


def needs(path, fps, width, height):
    i = _info(path)
    if not i:
        return False
    rate, w, h, codec = i
    # anything that is not already the program's own format plays lighter converted: another frame rate
    # (24 / 30 / 50 / 60p), another size (4K, square, letterbox) or a heavy codec (HEVC, ProRes, AV1...)
    return abs(rate - float(fps)) > 0.5 or (w, h) != (width, height) or codec != "h264"


def convert(path, fps, width, height, ffmpeg):
    """Queue a clip for converting; returns the copy's path (made in the background)."""
    global _thread
    name = os.path.basename(path)
    with _lock:
        if path in _queued:
            return target(path, fps)
        _queued.add(path)
        STATUS[name] = "waiting"
        _q.put((path, fps, width, height, ffmpeg))
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, daemon=True, name="clip converter")
            _thread.start()
    return target(path, fps)


def _command(ffmpeg, src, tmp, fps, width, height):
    enc = gpu.resolve("auto")
    vf = (f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1",
           "-hwaccel", "auto", "-i", src, "-map", "0:v:0", "-map", "0:a?", "-vf", vf, "-c:v", enc]
    if enc == "libx264":
        cmd += ["-preset", "veryfast", "-crf", "17", "-threads", "2"]
    else:
        cmd += ["-b:v", "20M", "-maxrate", "30M", "-bufsize", "40M"]
    gop = str(int(round(float(fps))))
    return cmd + ["-g", gop, "-bf", "0", "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
                  "-movflags", "+faststart", "-f", "mp4", tmp]


def _worker():
    while True:
        try:
            path, fps, width, height, ffmpeg = _q.get(timeout=30)
        except queue.Empty:
            return
        name = os.path.basename(path)
        try:
            _convert(path, fps, width, height, ffmpeg, name)
        except Exception as e:
            STATUS[name] = f"failed: {str(e)[:150]}"
            logging.info("clip converter: %s failed: %s", name, e)
        finally:
            with _lock:
                _queued.discard(path)


def _convert(path, fps, width, height, ffmpeg, name):
    dst = target(path, fps)
    tmp = dst[:-4] + ".part"
    total = 0.0
    try:
        with av.open(path) as c:
            total = (c.duration or 0) / 1_000_000
    except Exception:
        pass
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    STATUS[name] = "converting 0%"
    logging.info("clip converter: %s -> %s fps %dx%d", name, fps, width, height)
    p = subprocess.Popen(_command(ffmpeg, path, tmp, fps, width, height), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, creationflags=flags)
    try:
        import psutil
        psutil.Process(p.pid).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
    except Exception:
        pass
    err = []
    threading.Thread(target=lambda: err.extend(p.stderr.read().decode("utf-8", "replace").splitlines()),
                     daemon=True).start()
    for line in p.stdout:
        k, _, v = line.decode("ascii", "replace").strip().partition("=")
        if k == "out_time_us" and total > 0 and v.isdigit():
            STATUS[name] = f"converting {min(99, int(int(v) / 1e6 / total * 100))}%"
    if p.wait() != 0 or not os.path.isfile(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError((err[-1] if err else f"exit code {p.returncode}"))
    os.replace(tmp, dst)
    STATUS[name] = "ready"
    logging.info("clip converter: %s ready", name)
