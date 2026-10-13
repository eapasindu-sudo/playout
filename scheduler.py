"""Fires playlist items that have a fixed start time ("Exact Time")."""
import datetime as dt
import threading
import time

LATE_WINDOW = 30  # seconds: an item still fires if we are up to this late


class Scheduler:
    def __init__(self, engine):
        self.engine = engine
        self._fired = set()
        self._running = False

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True, name="scheduler").start()

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            try:
                self.check(dt.datetime.now())
            except Exception as e:
                self.engine.message = f"Scheduler error: {e}"
            time.sleep(0.1)

    def check(self, now):
        for item in list(self.engine.playlist):
            if not item.get("fixed"):
                continue
            try:
                at = dt.datetime.combine(now.date(), dt.time.fromisoformat(item["fixed"]))
            except ValueError:
                continue
            key = (item["id"], item["fixed"], now.date())
            late = (now - at).total_seconds()
            if 0 <= late <= LATE_WINDOW and key not in self._fired:
                self._fired.add(key)
                if self.engine.on_air != item["id"]:
                    self.engine.play(item["id"])


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class DayPlanner:
    """Loads each day's playlist by itself, and starts / stops scheduled recordings.

    settings (saved):
        day_lists   {"enabled": True, "time": "00:00",
                     "dates": {"2026-10-14": "Tuesday special"}, "weekdays": {"mon": "Weekday", ...}}
        rec_times   [{"id", "start": "HH:MM", "end": "HH:MM", "days": ["mon", ...] (empty = every day),
                      "date": "" (one date only), "enabled": True, "name": ""}]
    """

    def __init__(self, engine, store, recorder):
        self.engine = engine
        self.store = store
        self.recorder = recorder
        self.rec_by_schedule = None      # id of the schedule entry that started the running recording
        self.active = lambda: True       # day playlists off on a backup PC
        self._running = False
        self.last_error = ""

    def start(self):
        self._running = True
        threading.Thread(target=self._run, daemon=True, name="day planner").start()

    def stop(self):
        self._running = False

    @property
    def day_lists(self):
        d = self.store.data.get("day_lists") or {}
        d.setdefault("enabled", True)
        d.setdefault("time", "00:00")
        d.setdefault("dates", {})
        d.setdefault("weekdays", {})
        return d

    @property
    def rec_times(self):
        return self.store.data.get("rec_times") or []

    def list_for(self, day):
        d = self.day_lists
        return d["dates"].get(day.isoformat()) or d["weekdays"].get(DAYS[day.weekday()]) or ""

    def _run(self):
        while self._running:
            try:
                now = dt.datetime.now()
                self.check_day(now)
                self.check_rec(now)
            except Exception as e:
                self.last_error = str(e)
                self.engine.message = f"Schedule error: {e}"
            time.sleep(1)

    # ---- daily playlist ------------------------------------------------
    def check_day(self, now):
        d = self.day_lists
        if not d.get("enabled") or not self.active():
            return
        try:
            at = dt.time.fromisoformat(d.get("time") or "00:00")
        except ValueError:
            at = dt.time(0, 0)
        if now.time() < at:
            return
        today = now.date().isoformat()
        if self.store.data.get("day_list_loaded") == today:
            return
        name = self.list_for(now.date())
        self.store.set(day_list_loaded=today)
        if name:
            self.load(name)

    def load(self, name):
        try:
            items = self.store.load_list(name)
        except Exception as e:
            self.engine.message = f"Day playlist '{name}' could not be loaded: {e}"
            return False
        eng = self.engine
        eng.set_playlist(items)
        with eng._lock:
            if eng.playlist:
                eng.cued = eng.playlist[0]["id"]     # on-air item finishes, then the new day's list starts
                eng._changed()
        eng.message = f"Loaded today's playlist: {name}"
        return True

    # ---- scheduled recording -------------------------------------------
    @staticmethod
    def _active(entry, now):
        if not entry.get("enabled", True):
            return False
        try:
            s = dt.time.fromisoformat(entry["start"])
            e = dt.time.fromisoformat(entry["end"])
        except (KeyError, ValueError):
            return False
        t = now.time()
        if s <= e:
            on, day = s <= t < e, now.date()
        else:                                   # runs past midnight
            on = t >= s or t < e
            day = now.date() if t >= s else now.date() - dt.timedelta(days=1)
        if not on:
            return False
        if entry.get("date"):
            return entry["date"] == day.isoformat()
        days = entry.get("days") or []
        return not days or DAYS[day.weekday()] in days

    def check_rec(self, now):
        active = next((e for e in self.rec_times if self._active(e, now)), None)
        rec = self.recorder
        if active and not rec.recording and self.rec_by_schedule != active.get("id"):
            rec.start()
            self.rec_by_schedule = active.get("id")
            self.engine.message = f"Scheduled recording started ({active.get('name') or active['start'] + '-' + active['end']})"
        elif not active and self.rec_by_schedule is not None:
            if rec.recording:
                rec.stop()
                self.engine.message = "Scheduled recording finished"
            self.rec_by_schedule = None
        elif active and self.rec_by_schedule == active.get("id") and not rec.recording and rec.error:
            pass    # stopped by the operator or failed: do not start again in this slot
