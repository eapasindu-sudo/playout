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
