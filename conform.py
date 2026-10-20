"""Clips whose frame rate or size is above the program's (60p, 50p, 4K...) are converted once, in the
background, to the program's frame rate and size. Playout then plays the converted copy: decoding a
25p 1080 file costs a fraction of a 60p or 4K one, and no pictures are decoded only to be thrown away.

Converted copies live in <media folder>/.playout-converted/ and are made again when the clip changes.
Until a copy is ready (or if converting fails) the original file plays as before.
"""
import logging
import os
import queue
import subprocess
import sys
import threading

import av

import gpu

FOLDER = ".playout-converted"
ENABLED = True
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
                r = (float(rate) if rate else 0.0, v.codec_context.width, v.codec_context.height)
        except Exception:
            r = None
        _probe[key] = r
    return _probe[key]


def _target(path):
    folder, name = os.path.split(path)
    return os.path.join(folder, FOLDER, os.path.splitext(name)[0] + ".mp4")


def needs(path, fps, width, height):
    i = _info(path)
    if not i:
        return False
    rate, w, h = i
    return rate > float(fps) + 1 or h > height or w > width


def _fresh(src, dst):
    try:
        return os.path.getmtime(dst) >= os.path.getmtime(src)
    except OSError:
        return False


def path_for(path, fps, width, height, ffmpeg):
    """The file to play for this clip: the converted copy when it is ready, else the original
    (and the copy is queued). Never waits."""
    if not ENABLED or not os.path.isfile(path) or not needs(path, fps, width, height):
        return path
    dst = _target(path)
    if _fresh(path, dst):
        return dst
    queue_clip(path, fps, width, height, ffmpeg)
    return path


def queue_clip(path, fps, width, height, ffmpeg):
    global _thread
    name = os.path.basename(path)
    with _lock:
        if path in _queued or STATUS.get(name, "").startswith("failed"):
            return
        _queued.add(path)
        STATUS[name] = "waiting"
        _q.put((path, fps, width, height, ffmpeg))
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, daemon=True, name="clip converter")
            _thread.start()


def check_folder(folder, fps, width, height, ffmpeg):
    """Queue every clip in the media folder that would play better converted."""
    if not ENABLED:
        return
    try:
        names = os.listdir(folder)
    except OSError:
        return
    import media
    for n in names:
        p = os.path.join(folder, n)
        if os.path.isfile(p) and os.path.splitext(n)[1].lower() in media.MEDIA_EXT:
            path_for(p, fps, width, height, ffmpeg)


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
    dst = _target(path)
    if _fresh(path, dst):
        STATUS[name] = "ready"
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst[:-4] + ".part.mp4"
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
