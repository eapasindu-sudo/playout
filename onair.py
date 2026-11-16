"""Off-air guard: watches the program that goes out and raises an alarm when it is black, frozen or silent.

Every half second it takes a small sample of the program picture (about 64 x 36 points) and the sound
meters. When the picture stays black, stays the same (frozen) or the sound stays below the silence level
for longer than the set seconds, it:
  - shows it on the control screen and the Health page,
  - writes it to the air log,
  - sends a Telegram alert (event "offair"),
  - and, when set to, takes the next item if a clip is on air (a broken / black clip does not stay on air).

Colour bars, a paused clip, the failover filler and the emergency slate are what the operator put on
air, so they never raise an alarm.

settings (saved as "offair"):
    enabled        True
    black_secs     10     seconds of black before the alarm (0 = off)
    freeze_secs    15     seconds of a frozen picture (0 = off)
    silence_secs   20     seconds of silence (0 = off)
    silence_db     -50    below this (dBFS peak) is silence
    action         "alert" | "next"   next: a clip that goes black / freezes / is silent is skipped
"""
import datetime as dt
import logging
import threading
import time

import numpy as np

DEFAULTS = {"enabled": True, "black_secs": 10, "freeze_secs": 15, "silence_secs": 20, "silence_db": -50,
            "action": "alert"}
NAMES = {"black": "BLACK SCREEN", "freeze": "FROZEN PICTURE", "silence": "NO SOUND"}
QUIET_SOURCES = ("BARS", "PAUSE", "FILLER", "SLATE")    # on air on purpose: no alarm
BLACK_LUMA = 20         # average brightness below this (0-255) ...
BLACK_BRIGHT = 0.03     # ... with under 3 % of points brighter than 40 (a small logo over black still counts)
FREEZE_DIFF = 0.6       # average change per point (0-255) below this between two samples = the same picture


def clean_settings(s):
    out = dict(DEFAULTS)
    s = s or {}
    if "enabled" in s:
        out["enabled"] = bool(s["enabled"])
    for k, hi in (("black_secs", 600), ("freeze_secs", 600), ("silence_secs", 600)):
        try:
            out[k] = max(0, min(hi, int(float(s.get(k, out[k])))))
        except (TypeError, ValueError):
            pass
    try:
        out["silence_db"] = max(-90, min(-20, int(float(s.get("silence_db", out["silence_db"])))))
    except (TypeError, ValueError):
        pass
    if s.get("action") in ("alert", "next"):
        out["action"] = s["action"]
    return out


def luma_sample(frame, width, height, points=64):
    """A small brightness picture (rows x cols, float32 0-255) of a flat BGRA frame or a UYVY picture."""
    if frame is None:
        return None
    a = np.asarray(frame)
    if getattr(frame, "uyvy", False) and getattr(frame, "w", 0) and getattr(frame, "h", 0):
        w, h = frame.w, frame.h
        y = a.reshape(h, w * 2)[:, 1::2]                    # U Y V Y: every second byte is brightness
        sy, sx = max(1, h // (points * 9 // 16)), max(1, w // points)
        return y[::sy, ::sx].astype(np.float32)
    if a.size != width * height * 4:
        return None
    img = a.reshape(height, width, 4)
    sy, sx = max(1, height // (points * 9 // 16)), max(1, width // points)
    s = img[::sy, ::sx, :3].astype(np.float32)
    return s[..., 0] * 0.114 + s[..., 1] * 0.587 + s[..., 2] * 0.299


def is_black(luma):
    if luma is None or not luma.size:
        return False
    return float(luma.mean()) < BLACK_LUMA and float((luma > 40).mean()) < BLACK_BRIGHT


class Detector:
    """The timing logic, without the engine (so it can be tested): feed one sample at a time."""

    def __init__(self, settings=None):
        self.s = clean_settings(settings)
        self.reset()

    def reset(self):
        self.since = {"black": None, "freeze": None, "silence": None}
        self.active = {"black": False, "freeze": False, "silence": False}
        self._prev = None

    def feed(self, now, luma, peak_db, quiet=False):
        """-> list of (problem, True when it starts / False when it ends)."""
        changes = []
        if quiet or not self.s["enabled"]:
            for k in self.active:
                if self.active[k]:
                    changes.append((k, False))
            self.reset()
            return changes
        black = is_black(luma)
        frozen = False
        if luma is not None and self._prev is not None and self._prev.shape == luma.shape and not black:
            frozen = float(np.abs(luma - self._prev).mean()) < FREEZE_DIFF
        if luma is not None:
            self._prev = luma
        silent = peak_db is not None and peak_db < self.s["silence_db"]
        for k, cond in (("black", black), ("freeze", frozen), ("silence", silent)):
            limit = self.s[k + "_secs"]
            if not cond or not limit:
                self.since[k] = None
                if self.active[k]:
                    self.active[k] = False
                    changes.append((k, False))
                continue
            if self.since[k] is None:
                self.since[k] = now
            if not self.active[k] and now - self.since[k] >= limit:
                self.active[k] = True
                changes.append((k, True))
        return changes


class OffAirGuard:
    def __init__(self, engine, alerts=None):
        self.engine = engine
        self.alerts = alerts
        self.det = Detector()
        self.started = {}        # problem -> wall clock it started (air log)
        self.skipped = set()     # on-air item ids already skipped by the "next" action
        self.last_error = ""
        self.active = lambda: True       # a backup PC that follows the main: no alarms of its own

    @property
    def settings(self):
        return dict(self.det.s)

    def configure(self, s):
        self.det.s = clean_settings(s)
        self.det.reset()
        return self.settings

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="off-air guard").start()

    def status(self):
        on = [k for k, v in self.det.active.items() if v]
        return {"enabled": self.det.s["enabled"], "problems": on, "text": ", ".join(NAMES[k] for k in on)}

    def _run(self):
        while True:
            time.sleep(0.5)
            try:
                self.check(time.monotonic())
            except Exception as e:
                self.last_error = str(e)
                logging.info("off-air guard: %s", e)

    def _sample(self):
        e = self.engine
        src = e.last_source or ""
        quiet = (src in QUIET_SOURCES or e.state == "PAUSED" or getattr(e, "slate_on", False)
                 or bool(e.cg is not None and e.cg.stinger_active))
        frame = e.clean_frame if src.startswith("CLIP") or src.startswith("LIVE") else e.last_frame
        luma = luma_sample(frame, e.width, e.height)
        if luma is None:
            luma = luma_sample(e.last_frame, e.width, e.height)
        peak = max(e.meters) if e.meters else None
        return luma, peak, quiet

    def check(self, now):
        luma, peak, quiet = self._sample()
        quiet = quiet or not self.active()
        for problem, on in self.det.feed(now, luma, peak, quiet):
            self._report(problem, on)

    def _report(self, problem, on):
        e = self.engine
        item = e._item(e.on_air) if e.on_air else None
        what = (item or {}).get("name") or ("live" if (e.last_source or "").startswith("LIVE") else e.last_source or "program")
        name = NAMES[problem]
        if on:
            self.started[problem] = dt.datetime.now()
            secs = self.det.s[problem + "_secs"]
            e.message = f"{name} on air for {secs} s ({what})"
            if self.alerts:
                self.alerts.send("offair", f"{name} ON AIR for {secs} s: {what}", every=120)
            if (self.det.s["action"] == "next" and item is not None and item.get("type") == "clip"
                    and item["id"] not in self.skipped and problem in ("black", "freeze", "silence")):
                self.skipped.add(item["id"])
                e.message = f"{name}: '{what}' skipped, next item on air"
                logging.info("off-air guard: %s on %s, taking the next item", name, what)
                e.take_next()
                self.det.reset()
                for k, t in list(self.started.items()):
                    self._log(NAMES[k], t, "off-air alarm, clip skipped")
                self.started.clear()
        else:
            started = self.started.pop(problem, None)
            if started is not None:
                self._log(name, started, "off-air alarm")
            if self.alerts:
                self.alerts.send("offair", f"Program OK again ({name.lower()} ended)", every=60)
        if len(self.skipped) > 500:
            self.skipped.clear()

    def _log(self, name, started, status):
        try:
            self.engine.asrun.write("alarm", name, started, dt.datetime.now(), None, status, "", "", "")
        except Exception:
            pass
