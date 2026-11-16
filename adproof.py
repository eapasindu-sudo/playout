"""Ad proof clips: the first seconds of every commercial, recorded from the program output as it aired.

Each commercial that goes on air gets a small MP4 (640 x 360, 12.5 fps, no sound) with a band at the
bottom that shows the station, the date and time to the second, the client and the commercial's name.
Send it to the client as proof that the ad went out (with the logo / CGs that were on it).

Files: <logs>/proof/<YYYY-MM-DD>/<HH-MM-SS>_<client>_<name>.mp4, and index.jsonl in the same folder
(one line per clip: time, client, name, file, clip). The Ad Report downloads them as a zip.

settings (saved as "ad_proof"): enabled (False), secs (10; 0 = the whole commercial, up to 3 minutes)
"""
import datetime as dt
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import zipfile

import numpy as np

OUT_W = 640
MAX_SECS = 180
BAND = 30


def clean_settings(s):
    s = s or {}
    try:
        secs = max(0, min(MAX_SECS, int(float(s.get("secs", 10)))))
    except (TypeError, ValueError):
        secs = 10
    return {"enabled": bool(s.get("enabled", False)), "secs": secs}


def _safe(s, n=40):
    s = re.sub(r"[^\w\- ]+", "_", str(s or "")).strip()
    return (s or "-")[:n]


class _Job:
    """One proof clip: frames go to FFmpeg on its own thread (the output loop never waits)."""

    def __init__(self, ffmpeg, path, w, h, fps, frames, label):
        self.path, self.w, self.h = path, w, h
        self.left = frames
        self.label = label
        self.q = queue.Queue(maxsize=60)
        self.error = ""
        self._band = None
        self._band_key = None
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgra",
             "-s", f"{w}x{h}", "-r", f"{fps:g}", "-i", "-", "-c:v", "libx264", "-preset", "ultrafast",
             "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart", path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
        threading.Thread(target=self._write, daemon=True, name="ad proof").start()

    def band(self, now):
        key = now.replace(microsecond=0)
        if key != self._band_key:
            from PIL import Image, ImageDraw
            import textcg
            text = f"{now:%Y-%m-%d %H:%M:%S}   {self.label}"
            im = Image.new("RGBA", (self.w, BAND), (0, 0, 0, 255))
            d = ImageDraw.Draw(im)
            font = textcg.get_font(textcg._fontname({"font": "Arial", "text": text}), 16, True)
            d.text((8, BAND // 2), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
            self._band = np.asarray(im)[..., [2, 1, 0, 3]].copy()
            self._band_key = key
        return self._band

    def push(self, small):
        if self.left <= 0:
            return False
        self.left -= 1
        try:
            self.q.put_nowait(small)
        except queue.Full:
            pass            # a slow disk: drop a frame rather than hold the output
        return self.left > 0

    def close(self):
        self.left = 0
        try:
            self.q.put_nowait(None)
        except queue.Full:
            threading.Thread(target=lambda: self.q.put(None), daemon=True).start()

    def _write(self):
        try:
            while True:
                f = self.q.get()
                if f is None:
                    break
                self.proc.stdin.write(f.tobytes())
        except Exception as e:
            self.error = str(e)
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
            if self.proc.returncode:
                self.error = (self.proc.stderr.read() or b"").decode(errors="replace")[-300:]
        except Exception as e:
            self.error = self.error or str(e)
        try:
            self.proc.stderr.close()
        except Exception:
            pass
        if self.error:
            logging.info("ad proof %s: %s", self.path, self.error)


class AdProof:
    def __init__(self, engine, folder, ffmpeg):
        self.engine = engine
        self.folder = folder
        self.ffmpeg = ffmpeg
        self.station = ""
        self.s = clean_settings({})
        self.job = None
        self._item = None          # id of the clip on air
        self._done = None          # (id, start time) of the last commercial recorded
        self._k = 0
        self.count = 0
        self.last_error = ""

    def configure(self, s):
        self.s = clean_settings(s)
        if not self.s["enabled"] and self.job:
            self.job.close()
            self.job = None
        return dict(self.s)

    def info(self):
        return dict(self.s, recorded=self.count, error=self.last_error, folder=self.folder)

    # ---- called by the output loop for every program frame ------------------------------
    def push(self, video, source):
        if not self.s["enabled"]:
            return
        item_id = source[5:] if source.startswith("CLIP:") else None
        if item_id != self._item:
            if self.job:
                self.job.close()
                self.job = None
            self._item = item_id
            self._k = 0
            key = (item_id, getattr(self.engine, "item_started", None))
            if item_id and key != self._done:      # back after a pause / the slate: not recorded again
                it = self.engine._item(item_id)
                if it and (it.get("category") or "Commercial") == "Commercial" and not it.get("filler"):
                    self._done = key
                    self._start(it)
        if self.job is None or getattr(video, "uyvy", False):
            return
        self._k += 1
        if self._k % 2:
            return                  # every second frame (12.5 fps at 25)
        e = self.engine
        a = np.asarray(video)
        if a.size != e.width * e.height * 4:
            return
        step = max(1, e.width // OUT_W)
        small = a.reshape(e.height, e.width, 4)[::step, ::step][:self.job.h, :self.job.w].copy()
        small[-BAND:] = self.job.band(dt.datetime.now())
        if not self.job.push(small):
            self.job.close()
            self.job = None

    def _start(self, it):
        e = self.engine
        step = max(1, e.width // OUT_W)
        w, h = (e.width + step - 1) // step, (e.height + step - 1) // step
        w, h = w - w % 2, h - h % 2
        fps = float(e.fps) / 2
        secs = self.s["secs"] or min(MAX_SECS, float(it.get("duration") or MAX_SECS))
        now = dt.datetime.now()
        day = os.path.join(self.folder, f"{now:%Y-%m-%d}")
        client = (it.get("client") or "").strip()
        name = it.get("name") or it.get("file") or "commercial"
        fname = f"{now:%H-%M-%S}_{_safe(client)}_{_safe(name)}.mp4"
        label = " - ".join(x for x in (self.station, client, name) if x)
        try:
            os.makedirs(day, exist_ok=True)
            self.job = _Job(self.ffmpeg, os.path.join(day, fname), w, h, fps, int(secs * fps), label)
            with open(os.path.join(day, "index.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": now.isoformat(timespec="seconds"), "client": client, "name": name,
                                    "file": it.get("file", ""), "clip": fname}) + "\n")
            self.count += 1
            self.last_error = ""
        except Exception as ex:
            self.job = None
            self.last_error = f"Ad proof: {ex}"
            logging.info("ad proof: %s", ex)

    # ---- for the Ad Report -------------------------------------------------------------------
    def list(self, d1, d2, client=""):
        out = []
        day = d1
        while day <= d2:
            path = os.path.join(self.folder, f"{day:%Y-%m-%d}", "index.jsonl")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if client and r.get("client", "").lower() != client.lower():
                            continue
                        r["day"] = f"{day:%Y-%m-%d}"
                        r["exists"] = os.path.exists(os.path.join(self.folder, r["day"], r.get("clip", "")))
                        out.append(r)
            day += dt.timedelta(days=1)
        return out

    def path(self, day, clip):
        p = os.path.join(self.folder, os.path.basename(day), os.path.basename(clip))
        return p if os.path.isfile(p) else None

    def zip_to(self, fileobj, rows):
        with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_STORED) as z:
            lines = ["date,time,client,commercial,clip"]
            for r in rows:
                p = self.path(r["day"], r.get("clip", ""))
                if p:
                    z.write(p, f"{r['day']}/{r['clip']}")
                    lines.append(",".join('"' + str(x).replace('"', "'") + '"' for x in
                                          (r["day"], r["time"][11:], r.get("client", ""), r.get("name", ""), r["clip"])))
            z.writestr("proof-list.csv", "\n".join(lines) + "\n")
