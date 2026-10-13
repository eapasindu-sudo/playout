"""Alerts to a phone through Telegram: live input lost, stream dropped, Facebook 8-hour limit,
recording failed, Playout restarted, backup took over, disk almost full.

Setup: in Telegram talk to @BotFather, /newbot, copy the bot token into Settings > Alerts.
Then send any message to the new bot (or add it to a group) and press "Find chat".
"""
import json
import queue
import shutil
import socket
import threading
import time
import urllib.parse
import urllib.request

EVENTS = {
    "failover": "Live input lost / back",
    "stream": "Stream dropped / back",
    "health": "Stream health problem (slow upload, dropped frames, CPU)",
    "fb_limit": "Facebook 8-hour limit (30, 10, 5 min)",
    "record": "Recording failed / scheduled recording",
    "restart": "Playout restarted after a crash",
    "missing": "Playlist clip missing",
    "disk": "Disk almost full",
    "backup": "Backup PC took over / main is back",
}


class Alerts:
    def __init__(self):
        self.token = ""
        self.chat_id = ""
        self.enabled = False
        self.events = {k: True for k in EVENTS}
        self.station = ""
        self.last_error = ""
        self.sent = 0
        self._q = queue.Queue(maxsize=200)
        self._recent = {}
        threading.Thread(target=self._worker, daemon=True, name="alerts").start()

    def configure(self, enabled=None, token=None, chat_id=None, events=None):
        if enabled is not None:
            self.enabled = bool(enabled)
        if token is not None and token != "********":
            self.token = token.strip()
        if chat_id is not None:
            self.chat_id = str(chat_id).strip()
        if isinstance(events, dict):
            for k in EVENTS:
                if k in events:
                    self.events[k] = bool(events[k])
        return self.settings()

    def settings(self):
        return {"enabled": self.enabled, "token": self.token, "chat_id": self.chat_id, "events": dict(self.events)}

    def info(self):
        return {"enabled": self.enabled, "token_set": bool(self.token), "chat_id": self.chat_id,
                "events": dict(self.events), "names": EVENTS, "error": self.last_error, "sent": self.sent}

    def send(self, event, text, every=60):
        """Queue an alert. The same event + text is sent at most once per `every` seconds."""
        if not (self.enabled and self.token and self.chat_id and self.events.get(event, True)):
            return False
        key = (event, text)
        now = time.time()
        if now - self._recent.get(key, 0) < every:
            return False
        self._recent[key] = now
        head = f"{self.station or socket.gethostname()} - Playout"
        try:
            self._q.put_nowait(f"{head}\n{time.strftime('%H:%M:%S')}  {text}")
        except queue.Full:
            pass
        return True

    def _post(self, method, params):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return json.load(r)

    def _worker(self):
        while True:
            text = self._q.get()
            for attempt in range(3):
                try:
                    self._post("sendMessage", {"chat_id": self.chat_id, "text": text})
                    self.sent += 1
                    self.last_error = ""
                    break
                except Exception as e:
                    self.last_error = f"Telegram: {e}"
                    time.sleep(5 * (attempt + 1))

    def test(self):
        if not self.token or not self.chat_id:
            return {"ok": False, "error": "Set the bot token and find the chat first"}
        try:
            self._post("sendMessage", {"chat_id": self.chat_id, "text": f"{self.station or socket.gethostname()} - Playout\nTest alert: alerts work."})
            self.last_error = ""
            return {"ok": True}
        except Exception as e:
            self.last_error = f"Telegram: {e}"
            return {"ok": False, "error": self.last_error}

    def find_chat(self):
        """Chats that sent a message to the bot (newest first)."""
        if not self.token:
            return {"ok": False, "error": "Paste the bot token first"}
        try:
            r = self._post("getUpdates", {"limit": 50})
        except Exception as e:
            return {"ok": False, "error": f"Telegram: {e}"}
        chats = {}
        for u in r.get("result", []):
            m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            c = m.get("chat") or {}
            if c.get("id"):
                chats[str(c["id"])] = c.get("title") or " ".join(filter(None, [c.get("first_name"), c.get("last_name")])) or c.get("username") or str(c["id"])
        if not chats:
            return {"ok": False, "error": "No messages yet: send any message to the bot in Telegram, then press Find chat again"}
        return {"ok": True, "chats": [{"id": k, "name": v} for k, v in reversed(list(chats.items()))]}


class Watcher:
    """Looks at Playout's state every few seconds and raises alerts on changes."""

    def __init__(self, alerts, engine, streamer, recorder, folders):
        self.a = alerts
        self.engine = engine
        self.streamer = streamer
        self.recorder = recorder
        self.folders = folders
        self._fo = False
        self._dest = {}
        self._fb = {}
        self._rec_err = ""
        self._missing = set()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="alert watcher").start()

    def _run(self):
        n = 0
        while True:
            time.sleep(2)
            n += 1
            try:
                self.check(n)
            except Exception as e:
                self.a.last_error = f"watcher: {e}"

    def check(self, n):
        e, a = self.engine, self.a
        if e.failover_active != self._fo:
            self._fo = e.failover_active
            a.send("failover", "LIVE INPUT LOST - filler on air" if self._fo else "Live input is back on air", every=10)
        for d in self.streamer.info().get("destinations", []):
            slot, st = d["slot"], (d["status"] if d["enabled"] else "off")
            old = self._dest.get(slot)
            self._dest[slot] = st
            if old == "live" and st in ("error", "reconnecting"):
                a.send("stream", f"STREAM DROPPED: {d['name']} ({d.get('error') or st})", every=120)
            elif old in ("error", "reconnecting") and st == "live":
                a.send("stream", f"Stream back live: {d['name']}", every=30)
            if st == "live" and "facebook" in (d.get("url") or "").lower() and d.get("uptime"):
                left = 8 * 3600 - d["uptime"]
                for mins in (30, 10, 5):
                    if left <= mins * 60 and self._fb.get(slot, 999) > mins:
                        self._fb[slot] = mins
                        a.send("fb_limit", f"Facebook 8-hour limit: {int(left // 60)} minutes left on {d['name']}", every=1)
            elif st != "live":
                self._fb.pop(slot, None)
        err = self.recorder.error if not self.recorder.recording else ""
        if err and err != self._rec_err and "Recording stopped" in err:
            a.send("record", f"RECORDING FAILED: {err}", every=60)
        self._rec_err = err
        missing = {it["file"] for it in list(e.playlist) if it.get("type") == "clip" and not it.get("file_duration")}
        new = missing - self._missing
        if new:
            a.send("missing", "Clip missing in the playlist: " + ", ".join(sorted(new)[:5]), every=600)
        self._missing = missing
        if n % 150 == 1:      # every 5 minutes
            for f in self.folders:
                try:
                    free = shutil.disk_usage(f).free / 1e9
                except OSError:
                    continue
                if free < 10:
                    a.send("disk", f"Disk almost full: {free:.1f} GB free ({f})", every=3600)
                    break
