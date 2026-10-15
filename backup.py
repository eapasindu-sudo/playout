"""Main / backup PC: a second Playout PC follows the main one and takes over if it stops.

On the BACKUP PC (Settings > Backup PC: role "backup", address of the main PC, same sync key):
  - the playlist, Auto / Loop and the manual CG list are copied from the main PC every second,
  - clips and CG files the backup does not have are downloaded from the main PC,
  - the backup plays the same item at the same point (its NDI output matches the main's), so a
    switcher / vMix can change to it at any time,
  - if the main PC does not answer for a few seconds, the backup carries on by itself (it already
    plays the right thing), sends an alert, and can start its own live streams.
When the main PC is back, the backup follows it again (and stops the streams it started once the
main's streams are live).
"""
import json
import os
import threading
import time
import certs
import urllib.parse
import urllib.request

SYNC_FIELDS = ("id", "type", "name", "file", "duration", "fixed", "source", "cgs", "client", "in", "out", "gain_db", "category",
               "squeeze", "squeeze_at", "squeeze_for")


class Backup:
    def __init__(self, engine, cgm, streamer, media_dir, cg_dir, alerts=None):
        self.engine = engine
        self.cgm = cgm
        self.streamer = streamer
        self.media_dir = media_dir
        self.cg_dir = cg_dir
        self.alerts = alerts
        self.role = "off"              # off | main | backup
        self.main_url = ""
        self.key = ""
        self.takeover_after = 5.0
        self.take_streams = False
        self.mirror = False            # copy every media / CG file of the main PC, not only the playlist's
        self.mirror_info = ""
        self._mirror_at = 0.0
        self._mirror_left = 0
        self.status = "off"
        self.error = ""
        self.last_ok = 0.0
        self.main_down = False
        self.lag = None
        self._streams_started = False
        self._downloading = set()
        self._pl_sig = None
        self._cg_sig = None
        threading.Thread(target=self._run, daemon=True, name="backup sync").start()

    def configure(self, role=None, main_url=None, key=None, takeover_after=None, take_streams=None, mirror=None):
        if role in ("off", "main", "backup"):
            self.role = role
        if main_url is not None:
            u = main_url.strip().rstrip("/")
            if u and not u.startswith("http"):
                u = "http://" + u
            if u and u.count(":") < 2:
                u += ":8080"
            self.main_url = u
        if key is not None and key != "********":
            self.key = key.strip()
        if takeover_after not in (None, ""):
            self.takeover_after = max(2.0, min(60.0, float(takeover_after)))
        if take_streams is not None:
            self.take_streams = bool(take_streams)
        if mirror is not None:
            self.mirror = bool(mirror)
            self._mirror_at = 0.0
        self.status = {"off": "off", "main": "main PC (a backup can follow it)", "backup": "connecting to main..."}[self.role]
        self.main_down = False
        return self.settings()

    def settings(self):
        return {"role": self.role, "main_url": self.main_url, "key": self.key,
                "takeover_after": self.takeover_after, "take_streams": self.take_streams, "mirror": self.mirror}

    def info(self):
        return {**{k: v for k, v in self.settings().items() if k != "key"}, "key_set": bool(self.key),
                "status": self.status, "error": self.error, "main_down": self.main_down,
                "lag": self.lag, "downloading": sorted(self._downloading), "mirror_info": self.mirror_info}

    def key_ok(self, given):
        return bool(self.key) and given == self.key

    # ---- what the main PC sends -------------------------------------------
    def snapshot(self):
        e = self.engine
        with e._lock:
            items = [{k: it.get(k) for k in SYNC_FIELDS if k in it} for it in e.playlist]
            pos = e.item_ticks / float(e.fps) if e.on_air else 0.0
            snap = {"t": time.time(), "playlist": items, "on_air": e.on_air if e.state != "STOPPED" else None,
                    "state": e.state, "position": pos, "idle": e.idle, "auto": e.auto, "loop": e.loop,
                    "started": e.item_started is not None}
        cg = self.cgm.status()
        snap["manual_cgs"] = self.cgm.manual
        snap["manual_on"] = {k: v for k, v in cg["manual_on"].items() if v in ("in", "on")}
        snap["streams_live"] = any(d["enabled"] and d["status"] == "live" for d in self.streamer.info()["destinations"])
        return snap

    # ---- backup side -----------------------------------------------------
    def _get(self, path, timeout=3):
        req = urllib.request.Request(self.main_url + path, headers={"X-Playout-Key": self.key})
        return certs.urlopen(req, timeout=timeout)

    def _run(self):
        while True:
            time.sleep(1)
            if self.role != "backup" or not self.main_url:
                continue
            try:
                t0 = time.time()
                with self._get("/api/sync") as r:
                    snap = json.load(r)
                self.lag = round((time.time() - t0) / 2, 3)
                self.last_ok = time.time()
                self.error = ""
                if self.main_down:
                    self.main_down = False
                    self._alert("Main PC is back - backup follows it again")
                self.follow(snap)
                if self.mirror and time.time() - self._mirror_at > (5 if self._mirror_left else 30):
                    self._mirror_at = time.time()
                    self.mirror_files()
                self.status = "following main" + (f" - downloading {len(self._downloading)} file(s)" if self._downloading else "")
            except Exception as ex:
                self.error = str(ex)[:200]
                down_for = time.time() - (self.last_ok or time.time())
                if self.last_ok and down_for >= self.takeover_after and not self.main_down:
                    self.takeover()
                elif not self.last_ok:
                    self.status = "cannot reach the main PC"

    def takeover(self):
        self.main_down = True
        self.status = "MAIN PC DOWN - backup is on air by itself"
        self.engine.message = "MAIN PC DOWN - backup carries on"
        msg = "MAIN PC DOWN - backup PC took over"
        if self.take_streams:
            started = 0
            for d in self.streamer.dests:
                if d.url.strip() and not d.enabled:
                    self.streamer.set_enabled(d.slot, True)
                    started += 1
            self._streams_started = started > 0
            if started:
                msg += f" and started {started} live stream(s)"
        self._alert(msg)

    def _alert(self, text):
        if self.alerts is not None:
            self.alerts.send("backup", text, every=30)

    def follow(self, s):
        e = self.engine
        # playlist
        sig = json.dumps(s["playlist"], sort_keys=True)
        if sig != self._pl_sig:
            self._pl_sig = sig
            e.set_playlist(s["playlist"])
            for it in s["playlist"]:
                if it.get("type") == "clip" and it.get("file"):
                    self._fetch("media", it["file"], self.media_dir)
                for c in it.get("cgs") or []:
                    if c.get("file"):
                        self._fetch("cg", c["file"], self.cg_dir)
                if it.get("squeeze"):
                    self._fetch("cg", it["squeeze"], self.cg_dir)
        if e.auto != s["auto"] or e.loop != s["loop"]:
            e.set_modes(s["auto"], s["loop"])
        # manual CGs
        csig = json.dumps(s.get("manual_cgs"), sort_keys=True)
        if csig != self._cg_sig:
            self._cg_sig = csig
            for c in s.get("manual_cgs") or []:
                if c.get("file"):
                    self._fetch("cg", c["file"], self.cg_dir)
            self.cgm.set_manual(s.get("manual_cgs") or [])
        mine = {k for k, v in self.cgm.status()["manual_on"].items() if v in ("in", "on")}
        theirs = set(s.get("manual_on") or {})
        for cid in theirs - mine:
            self.cgm.take_in(cid)
        for cid in mine - theirs:
            self.cgm.take_out(cid)
        # what is on air
        target = s.get("on_air")
        if s["state"] == "PLAYING" and target and s.get("started"):
            pos = float(s["position"]) + (self.lag or 0)
            ours = e.on_air if e.state == "PLAYING" else None
            our_pos = e.item_ticks / float(e.fps) if ours else -99
            if ours != target or abs(our_pos - pos) > 1.0:
                e.resume(target, pos, message="Following the main PC")
        elif s["state"] == "PAUSED":
            if e.state == "PLAYING" and e.player is not None:
                e.pause()
        elif s["state"] == "STOPPED" and e.state != "STOPPED":
            e.stop() if s["idle"] == "bars" else e.take_live()
        elif s["state"] == "STOPPED" and e.idle != s["idle"]:
            e.stop() if s["idle"] == "bars" else e.take_live()
        # streams the backup started while the main was down
        if self._streams_started and s.get("streams_live"):
            for d in self.streamer.dests:
                if d.enabled:
                    self.streamer.set_enabled(d.slot, False)
            self._streams_started = False
            self._alert("Main PC streams are live again - backup streams stopped")

    def files(self):
        """Main PC side: every media and CG file (name, size) for a backup that mirrors them all."""
        out = {}
        for kind, folder in (("media", self.media_dir), ("cg", self.cg_dir)):
            items = []
            for n in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
                p = os.path.join(folder, n)
                if os.path.isfile(p) and not n.endswith(".part"):
                    items.append({"name": n, "size": os.path.getsize(p)})
            out[kind] = items
        return out

    def mirror_files(self):
        """Backup PC side: download every file the main PC has that this PC does not (or has a different
        size of). Nothing is ever deleted on the backup PC."""
        try:
            with self._get("/api/sync/files", timeout=10) as r:
                lists = json.load(r)
        except Exception as ex:
            self.mirror_info = f"file list not read: {ex}"[:200]
            return
        missing = 0
        for kind, folder in (("media", self.media_dir), ("cg", self.cg_dir)):
            for f in lists.get(kind) or []:
                p = os.path.join(folder, f["name"])
                if not os.path.exists(p) or os.path.getsize(p) != f["size"]:
                    missing += 1
                    if len(self._downloading) < 2:          # two at a time, the rest on the next round
                        self._fetch(kind, f["name"], folder, replace=True)
        self._mirror_left = missing
        total = sum(len(v or []) for v in lists.values())
        self.mirror_info = (f"copying {missing} of {total} files" if missing
                            else f"all {total} files copied ({time.strftime('%H:%M:%S')})")

    def _fetch(self, kind, name, folder, replace=False):
        """Download a file the backup does not have (in the background)."""
        dest = os.path.join(folder, name)
        if (os.path.exists(dest) and not replace) or name in self._downloading or os.sep in name or "/" in name:
            return
        self._downloading.add(name)

        def run():
            try:
                with self._get(f"/api/sync/file/{kind}/{urllib.parse.quote(name)}", timeout=30) as r, \
                        open(dest + ".part", "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                os.replace(dest + ".part", dest)
                self._pl_sig = None      # re-read the playlist so durations fill in
            except Exception as ex:
                self.error = f"download {name}: {ex}"
                try:
                    os.remove(dest + ".part")
                except OSError:
                    pass
            finally:
                self._downloading.discard(name)

        threading.Thread(target=run, daemon=True, name=f"fetch {name}").start()
