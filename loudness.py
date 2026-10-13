"""Audio normalize: measures each clip's loudness (EBU R128) once and works out a gain.

Commercials come in at very different levels. With normalize on, every clip is brought to
the same loudness (default -23 LUFS, the EBU broadcast level) while it plays, without
changing the file. Measuring runs in the background with FFmpeg's ebur128 filter; results
are cached in data/loudness.json (by file name, size and date).
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading

MAX_BOOST = 12.0     # dB: never lift a quiet clip more than this
MAX_CUT = 30.0
PEAK_CEILING = -1.0  # dBTP: a boost never pushes peaks above this


class Loudness:
    def __init__(self, data_dir, ffmpeg):
        self.ffmpeg = ffmpeg
        self.path = os.path.join(data_dir, "loudness.json")
        self.enabled = True
        self.target = -23.0
        self._lock = threading.Lock()
        self._cache = {}
        self._q = queue.Queue()
        self._queued = set()
        self.busy = ""
        try:
            with open(self.path, encoding="utf-8") as f:
                self._cache = json.load(f)
        except Exception:
            self._cache = {}
        threading.Thread(target=self._worker, daemon=True, name="loudness").start()

    def configure(self, enabled=None, target=None):
        if enabled is not None:
            self.enabled = bool(enabled)
        if target not in (None, ""):
            self.target = max(-35.0, min(-8.0, float(target)))
        return self.settings()

    def settings(self):
        return {"normalize": self.enabled, "target": self.target}

    @staticmethod
    def _key(path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        return f"{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}"

    def info(self, path):
        """{'lufs': -18.2, 'peak': -0.5} or None when not measured yet."""
        k = self._key(path)
        with self._lock:
            return self._cache.get(k) if k else None

    def request(self, path):
        """Measure in the background if not known yet."""
        k = self._key(path)
        if not k:
            return
        with self._lock:
            if k in self._cache or k in self._queued:
                return
            self._queued.add(k)
        self._q.put((k, path))

    def gain_db(self, path):
        """dB to apply while playing (0 when normalize is off or the clip is not measured yet)."""
        if not self.enabled:
            return 0.0
        m = self.info(path)
        if not m or m.get("lufs") is None:
            self.request(path)
            return 0.0
        g = self.target - m["lufs"]
        if m.get("peak") is not None and g > 0:
            g = min(g, PEAK_CEILING - m["peak"])
        return max(-MAX_CUT, min(MAX_BOOST, g))

    # ---- worker ----------------------------------------------------------
    def _worker(self):
        while True:
            k, path = self._q.get()
            self.busy = os.path.basename(path)
            try:
                res = measure(self.ffmpeg, path)
            except Exception as e:
                res = {"lufs": None, "peak": None, "error": str(e)[:200]}
            with self._lock:
                self._cache[k] = res
                self._queued.discard(k)
                try:
                    tmp = self.path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(self._cache, f, indent=0)
                    os.replace(tmp, self.path)
                except Exception:
                    pass
            self.busy = ""


def measure(ffmpeg, path):
    """Integrated loudness (LUFS) and true peak (dBTP) of a file's sound."""
    flags = 0x08000000 if sys.platform == "win32" else 0     # CREATE_NO_WINDOW
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-i", path, "-vn", "-sn", "-dn",
           "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=600, creationflags=flags)
    out = p.stderr
    tail = out[out.rfind("Summary:"):] if "Summary:" in out else out
    m = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", tail)
    pk = re.search(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS", tail)
    lufs = float(m.group(1)) if m and m.group(1) != "-inf" else None
    peak = float(pk.group(1)) if pk and pk.group(1) != "-inf" else None
    if lufs is not None and lufs < -70:
        lufs = None     # silent clip: leave it alone
    return {"lufs": lufs, "peak": peak}
