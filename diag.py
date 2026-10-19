"""Diagnostics file: one zip with what is needed to see what Playout is doing on this PC (version, PC and
graphics card, health, CPU per part, web CG pages, settings without passwords / keys, and the recent log),
so it can be sent to support. Nothing secret goes in it."""
import collections
import datetime as dt
import io
import json
import logging
import os
import platform
import re
import sys
import threading
import traceback
import zipfile

_SECRET = re.compile(r"key|token|pass|secret|auth|cookie", re.I)
_lines = collections.deque(maxlen=4000)


class _Ring(logging.Handler):
    def emit(self, record):
        try:
            _lines.append(self.format(record))
        except Exception:
            pass


def install():
    """Keep the recent log lines (INFO and up) in memory for the diagnostics file."""
    h = _Ring(logging.INFO)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
        for other in root.handlers:          # the console / log file keeps showing warnings only
            if other is not h and other.level == logging.NOTSET:
                other.setLevel(logging.WARNING)


def redact(v, key=""):
    """Settings with passwords, keys and tokens taken out (stream URLs lose their key part)."""
    if isinstance(v, dict):
        return {k: redact(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x, key) for x in v]
    if isinstance(v, str) and v:
        if _SECRET.search(key or ""):
            return "(set)"
        if re.match(r"(rtmps?|srt)://", v, re.I):
            return re.sub(r"(rtmps?://[^/]+/[^/]+/).*", r"\1(key removed)", v.split("?")[0], flags=re.I)
        return re.sub(r"(?i)([?&](key|token|pass\w*|secret|auth)=)[^&]+", r"\1(removed)", v)
    return v


def _try(fn):
    try:
        return fn()
    except Exception as e:
        return f"(could not read: {e})"


def _tail(path, size=2_000_000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - size))
            return f.read().decode("utf-8", "replace")
    except OSError as e:
        return f"(no file: {e})"


def build(home, cfg, store_data, health, sysmon, engine=None, streamer=None):
    """The diagnostics zip as bytes."""
    import gpu
    import gpucg
    import gpudriver
    import media
    import version
    import webcg
    info = {
        "made": dt.datetime.now().isoformat(timespec="seconds"),
        "version": version.VERSION,
        "windows": platform.platform(), "python": sys.version.split()[0],
        "cpu": platform.processor(), "cores": os.cpu_count(),
        "graphics_cards": _try(lambda: gpudriver.STATE),
        "encoders": _try(gpu.info), "encoder_errors": dict(gpu.ERRORS),
        "cg_blending": dict(gpucg.STATE), "picture_scaling": dict(gpucg.CONVERT),
        "video_decoding": dict(media.DECODE),
        "computer": _try(lambda: dict(sysmon.stats)),
        "stream": redact(_try(lambda: streamer.info())) if streamer else None,
        "web_cg_pages": [{"url": redact(r.url), "loaded": r.loaded, "on_air": r.active, "pictures": r.frames,
                          "error": r.error, "size": [r.width, r.height]} for r in list(webcg.LIVE)],
        "threads": sorted(t.name for t in threading.enumerate()),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.json", json.dumps(info, indent=1, default=str))
        z.writestr("health.json", json.dumps(_try(health.get), indent=1, default=str))
        z.writestr("settings.json", json.dumps(redact(dict(cfg)), indent=1, default=str))
        z.writestr("saved.json", json.dumps(redact(dict(store_data)), indent=1, default=str))
        if engine is not None:
            z.writestr("playout.json", json.dumps(_try(engine.snapshot), indent=1, default=str))
        z.writestr("recent-log.txt", "\n".join(_lines))
        z.writestr("playout.log", _tail(os.path.join(home, "logs", "playout.log")))
        stacks = sys._current_frames()
        z.writestr("threads.txt", "\n\n".join(
            f"--- {t.name}\n" + "".join(traceback.format_stack(stacks[t.ident]))
            for t in threading.enumerate() if t.ident in stacks))
    return buf.getvalue()
