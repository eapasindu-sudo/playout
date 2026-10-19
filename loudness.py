"""Audio normalize and clip check: measures each clip's loudness (EBU R128) once and works out a gain.

Commercials come in at very different levels. With normalize on, every clip is brought to
the same loudness (default -23 LUFS, the EBU broadcast level) while it plays, without
changing the file. Measuring runs in the background with FFmpeg's ebur128 filter; results
are cached in data/loudness.json (by file name, size and date).

The same pass checks the clip for problems (see warnings()): black or silent parts, no sound,
a picture size or frame rate that does not match the output, interlaced video.
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
        """{'lufs': -18.2, 'peak': -0.5, 'qc': {...}} or None when not measured yet."""
        k = self._key(path)
        with self._lock:
            return self._cache.get(k) if k else None

    def checked(self, path):
        """The clip check is done for this file (older caches have loudness only)."""
        m = self.info(path)
        return bool(m and "qc" in m)

    def request(self, path):
        """Measure in the background if not known yet."""
        k = self._key(path)
        if not k:
            return
        with self._lock:
            if (k in self._cache and "qc" in self._cache[k]) or k in self._queued:
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
                res = {"lufs": None, "peak": None, "qc": {}, "error": str(e)[:200]}
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


def _probe(path):
    """Picture size, frame rate, interlacing and sound tracks of a file."""
    import av
    out = {"w": 0, "h": 0, "fps": 0.0, "interlaced": False, "audio": 0, "duration": 0.0}
    with av.open(path) as c:
        if c.duration:
            out["duration"] = c.duration / 1e6
        out["audio"] = len(c.streams.audio)
        if c.streams.video:
            vs = c.streams.video[0]
            out["w"], out["h"] = vs.codec_context.width, vs.codec_context.height
            rate = vs.average_rate or vs.guessed_rate or vs.base_rate
            out["fps"] = round(float(rate), 3) if rate else 0.0
            if vs.frames == 1 or (vs.codec_context.name in ("png", "mjpeg") and not out["duration"]):
                out["still"] = True
            try:
                for fr in c.decode(vs):
                    out["interlaced"] = bool(getattr(fr, "interlaced_frame", False))
                    break
            except Exception:
                pass
    return out


def _spans(text, start_re, end_re, duration):
    """[(start, end)] from FFmpeg blackdetect / silencedetect log lines."""
    spans, open_at = [], None
    for line in text.splitlines():
        m = re.search(start_re, line)
        if m:
            open_at = float(m.group(1))
        m = re.search(end_re, line)
        if m and open_at is not None:
            spans.append((round(open_at, 2), round(float(m.group(1)), 2)))
            open_at = None
    if open_at is not None:
        spans.append((round(open_at, 2), round(duration or open_at, 2)))
    return spans


def measure(ffmpeg, path):
    """Integrated loudness (LUFS), true peak (dBTP) and the clip check (black / silent parts, format)."""
    # CREATE_NO_WINDOW + BELOW_NORMAL_PRIORITY_CLASS: the check never slows down what is on air
    flags = (0x08000000 | 0x00004000) if sys.platform == "win32" else 0
    try:
        info = _probe(path)
    except Exception:
        info = {"w": 0, "h": 0, "fps": 0.0, "interlaced": False, "audio": 0, "duration": 0.0}
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-filter_threads", "1"]
    if info["w"] and not info.get("still"):
        # the picture is decoded on the graphics card when there is one (one CPU thread otherwise), and only
        # 5 small pictures a second are checked for black: the check stays light while something plays
        cmd += ["-hwaccel", "auto", "-threads", "1", "-i", path, "-sn", "-dn",
                "-vf", "fps=5,scale=320:-2,blackdetect=d=1:pix_th=0.10"]
    else:
        cmd += ["-threads", "1", "-i", path, "-sn", "-dn", "-vn"]
    cmd += ["-af", "ebur128=peak=true:framelog=quiet,silencedetect=n=-50dB:d=3", "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=1800, creationflags=flags)
    out = p.stderr
    tail = out[out.rfind("Summary:"):] if "Summary:" in out else out
    m = re.search(r"I:\s+(-?[\d.]+|-inf)\s+LUFS", tail)
    pk = re.search(r"Peak:\s+(-?[\d.]+|-inf)\s+dBFS", tail)
    lufs = float(m.group(1)) if m and m.group(1) != "-inf" else None
    peak = float(pk.group(1)) if pk and pk.group(1) != "-inf" else None
    if lufs is not None and lufs <= -69.9:
        lufs = None     # silent clip: leave it alone
    dur = info.get("duration") or 0.0
    qc = dict(info)
    qc["black"] = _spans(out, r"black_start:\s*(-?[\d.]+)", r"black_end:\s*(-?[\d.]+)", dur)
    qc["silence"] = _spans(out, r"silence_start:\s*(-?[\d.]+)", r"silence_end:\s*(-?[\d.]+)", dur) if info["audio"] else []
    return {"lufs": lufs, "peak": peak, "qc": qc}


def _t(sec):
    sec = max(0, int(round(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


def warnings(res, width=1920, height=1080, fps=25.0):
    """Plain-language problems found by the clip check (empty list = clip is fine)."""
    if not res or "qc" not in res:
        return []
    q = res["qc"] or {}
    if res.get("error") and not q:
        return ["Could not check this file: " + res["error"][:120]]
    out = []
    dur = q.get("duration") or 0.0
    if not q.get("w"):
        out.append("No picture (sound only)")
    else:
        w, h = q["w"], q["h"]
        if h and abs(w / h - width / height) > 0.03:
            out.append(f"Picture is {w}x{h}, not {width}x{height} shape: black bars on the sides / top")
        elif h and h < height * 0.66:
            out.append(f"Low resolution {w}x{h}: looks soft at {width}x{height}")
        f = q.get("fps") or 0
        if f and not q.get("still") and not any(abs(f - fps * k) < 0.05 for k in (0.5, 1, 2)):
            out.append(f"{f:g} fps clip on a {fps:g} fps output: movement may judder")
        if q.get("interlaced"):
            out.append("Interlaced video: lines may show on movement")
    if not q.get("audio"):
        out.append("No sound track")
    elif res.get("lufs") is None and res.get("peak") in (None, float("-inf")):
        out.append("Sound track is silent")
    else:
        for a, b in q.get("silence") or []:
            if dur and a <= 0.5 and b >= dur - 0.5:
                out.append("Sound track is silent")
                break
            out.append(f"Silent {_t(a)} to {_t(b)}")
    for a, b in q.get("black") or []:
        if dur and a <= 0.5 and b >= dur - 0.5:
            out.append("Picture is black all the way")
            break
        out.append(f"Black picture {_t(a)} to {_t(b)}")
    if res.get("lufs") is not None and res["lufs"] > -9:
        out.append(f"Very loud ({res['lufs']:.0f} LUFS)")
    return out[:8]
