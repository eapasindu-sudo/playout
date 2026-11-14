"""CG Composer Live: designs played to air from playlists, with its own outputs.

The operator cues a design (Preview), takes it (Live: the old one goes out as the new one comes in, each
layer with its own animation), clears it, or steps Next / Previous through the playlist. Fields (names,
scores) can be changed while it is on air.

Outputs:
  - NDI with alpha (vMix, OBS with the NDI plugin, Playout's NDI key, a TriCaster...)
  - /overlay: a see-through web page for an OBS / vMix Browser Source; also the full-screen output window
    (with a colour behind it) and the Live monitor. It shows the same layer pictures, moved by this engine.
  - HTTP control for Stream Deck / Companion / vMix scripts: /trigger/take, /trigger/clear, /trigger/next ...

Everything (playlists, outputs) is kept in composer-live.json in the Playout folder after every change,
so a crash or a restart brings it all back (what was on air is cued again, not put back on air).
"""
import copy
import io
import json
import logging
import os
import socket
import threading
import time
import uuid
import zipfile
from fractions import Fraction

import numpy as np

import design

PACKAGE_EXT = ".cgpack"
RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080), "4k": (3840, 2160)}
FPS = (25, 30, 50, 60)


def _new_id():
    return uuid.uuid4().hex[:8]


class Program:
    """One design on the Live output: its layers, and where it is in coming in / being on / going out."""

    def __init__(self, item, w, h, fps, anim=None):
        self.item = item
        self.id = _new_id()                  # the overlay page knows a design by this
        self.anim = anim or design.Animator(item, w, h, fps)
        self.revs = [0] * len(self.anim.layers)   # a layer changed on air (new text, a timer) -> its rev goes up
        self.phase, self.t0 = "in", time.monotonic()

    def states(self, now):
        t = now - self.t0
        if self.phase == "in" and t >= self.anim.length("in"):
            self.phase = "on"
        if self.phase == "out" and t >= self.anim.length("out"):
            self.phase = "gone"
        if self.phase == "gone":
            return []
        return self.anim.state(self.phase, t)

    @property
    def moving(self):
        return self.phase in ("in", "out")

    def go_out(self):
        if self.phase in ("in", "on"):
            self.phase, self.t0 = "out", time.monotonic()


class Live:
    def __init__(self, home, folder, playout_port=8080):
        self.playout_port = playout_port     # Playout on this PC: its NDI key shows our output on its program
        self.link_msg = ""
        self._linking = False
        self.file = os.path.join(home, "composer-live.json")
        self.folder = folder                 # the CG folder (pictures, the CG library)
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.s = self._load()
        self._polled = {}                    # item id -> time its data link was last read
        self.data_errors = {}
        self.atem_msg = ""
        self._anims = {}                     # (item id, rev, w, h, fps) -> design.Animator, made ahead of the take
        self.progs = []                      # Program on air (the newest last; older ones going out)
        self.cued = None                     # item id on Preview
        self.live_item = None                # item id on Live
        self.version = 0                     # overlay pages reload the layer pictures when this changes
        self.frame = None                    # last output picture (BGRA numpy, straight alpha) for NDI
        self.ndi = None
        self.ndi_error = ""
        self.stop = False
        self._sent = 0
        self.recovered = self.s.pop("was_live", None)
        if self.recovered and self._find(self.recovered)[1] is not None:
            self.cued = self.recovered       # after a crash: what was on air is ready to take again
        else:
            self.recovered = None
        self._apply_outputs()
        threading.Thread(target=self._run, daemon=True, name="cg live").start()
        threading.Thread(target=self._tick, daemon=True, name="cg live timers").start()

    # ---- saved state ---------------------------------------------------
    def _load(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            s = {}
        s.setdefault("playlists", [{"id": _new_id(), "name": "Playlist 1", "items": []}])
        if not s["playlists"]:
            s["playlists"].append({"id": _new_id(), "name": "Playlist 1", "items": []})
        s.setdefault("active", s["playlists"][0]["id"])
        s.setdefault("res", "1080p")
        s.setdefault("fps", 50)
        s.setdefault("ndi", {"on": False, "name": "ENDORA CG COMPOSER"})
        s.setdefault("trans", {"type": "", "dur": 0.5})   # Take transition for every design ("" = each design's own)
        s.setdefault("playout", True)            # Take also puts it on Playout's program (NDI key on this PC)
        return s

    def _save(self):
        tmp = self.file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.s, f, ensure_ascii=False)
            os.replace(tmp, self.file)
        except Exception:
            logging.exception("could not save %s", self.file)

    @property
    def size(self):
        return RESOLUTIONS.get(self.s.get("res"), (1920, 1080))

    @property
    def fps(self):
        f = int(self.s.get("fps") or 50)
        return f if f in FPS else 50

    # ---- playlists -----------------------------------------------------
    def _pl(self, pid=None):
        pid = pid or self.s["active"]
        return next((p for p in self.s["playlists"] if p["id"] == pid), self.s["playlists"][0])

    def _find(self, item_id):
        for p in self.s["playlists"]:
            for it in p["items"]:
                if it["id"] == item_id:
                    return p, it
        return None, None

    def state(self):
        with self.lock:
            on = self.progs[-1] if self.progs and self.progs[-1].phase in ("in", "on") else None
            return {"playlists": self.s["playlists"], "active": self.s["active"], "cued": self.cued,
                    "live": on.item["id"] if on else None, "phase": on.phase if on else "",
                    "res": self.s["res"], "fps": self.fps, "size": self.size, "recovered": self.recovered,
                    "playout": {"on": bool(self.s.get("playout")), "msg": self.link_msg}, "trans": self.s["trans"],
                    "atem": {**(self.s.get("atem") or {"on": False, "key": 0}), "msg": self.atem_msg},
                    "data_errors": dict(self.data_errors), "now": time.time(),
                    "ndi": {**self.s["ndi"], "error": self.ndi_error,
                            "connections": self.ndi.connections() if self.ndi else 0}}

    def add_items(self, cfgs, pid=None):
        with self.lock:
            pl = self._pl(pid)
            added = []
            for c in cfgs:
                it = copy.deepcopy(c)
                it["id"] = _new_id()
                it.setdefault("fields", {})
                pl["items"].append(it)
                added.append(it["id"])
            self._save()
            return added

    def playlist(self, op, pid=None, name=None, order=None):
        with self.lock:
            if op == "add":
                p = {"id": _new_id(), "name": (name or "").strip() or f"Playlist {len(self.s['playlists']) + 1}", "items": []}
                self.s["playlists"].append(p)
                self.s["active"] = p["id"]
            elif op == "rename":
                self._pl(pid)["name"] = (name or "").strip() or self._pl(pid)["name"]
            elif op == "delete" and len(self.s["playlists"]) > 1:
                self.s["playlists"] = [p for p in self.s["playlists"] if p["id"] != pid]
                if self.s["active"] == pid:
                    self.s["active"] = self.s["playlists"][0]["id"]
            elif op == "select":
                self.s["active"] = self._pl(pid)["id"]
            elif op == "order" and order:
                pl = self._pl(pid)
                by = {it["id"]: it for it in pl["items"]}
                pl["items"] = [by[i] for i in order if i in by] + [it for it in pl["items"] if it["id"] not in order]
            self._save()

    def item(self, op, item_id, changes=None):
        with self.lock:
            pl, it = self._find(item_id)
            if it is None:
                return "item not found"
            if op == "delete":
                pl["items"].remove(it)
                if self.cued == item_id:
                    self.cued = None
            elif op == "duplicate":
                c = copy.deepcopy(it)
                c["id"] = _new_id()
                c["name"] = (it.get("name") or "Design") + " copy"
                pl["items"].insert(pl["items"].index(it) + 1, c)
            elif op == "update":
                for k in ("name", "fields", "anim", "fade", "design"):
                    if k in (changes or {}):
                        it[k] = changes[k]
                it["rev"] = int(it.get("rev") or 0) + 1      # pictures of it made again
                if self.live_item == item_id and self.progs and self.progs[-1].phase in ("in", "on"):
                    self._update_on_air(it)
            self._save()
            return ""

    def _update_on_air(self, it, refresh_whole=True):
        """New text on air without a new take: only the layers whose picture changed are swapped (the page loads
        just those). A design whose layers changed (one appeared or went) is made again as a whole."""
        p = self.progs[-1] if self.progs else None
        if p is None or p.phase not in ("in", "on"):
            return
        try:
            new = self._make_anim(it, timers=True)
        except Exception:
            logging.exception("cg live: update on air")
            return
        ids = lambda a: [L.get("id") or L.get("name") for L, *_ in a.layers]
        if ids(new) != ids(p.anim):
            if refresh_whole:
                self._refresh_live(it)
            return
        changed = []
        for i, ((_L, im, x, y), (_L2, im2, x2, y2)) in enumerate(zip(p.anim.layers, new.layers)):
            if (x, y, im.size) != (x2, y2, im2.size) or im.tobytes() != im2.tobytes():
                changed.append(i)
        if not changed:
            return
        a = copy.copy(p.anim)                     # the cached animator stays as it was
        a.layers = list(p.anim.layers)
        a.pngs = dict(getattr(p.anim, "pngs", {}))
        for i in changed:
            a.layers[i] = (a.layers[i][0],) + tuple(new.layers[i][1:])
            a.pngs.pop(i, None)
            p.revs[i] += 1
        p.anim = a
        p.item = copy.deepcopy(it)
        self.version += 1
        self.cv.notify_all()

    def _refresh_live(self, it):
        """New text on air at once (no animation): the design is made again and stays on."""
        w, h = self.size
        p = Program(it, w, h, self.fps, self._animator(it))
        p.phase = "on"
        self.progs = [p]
        self.version += 1
        self.cv.notify_all()

    # ---- on air --------------------------------------------------------
    def cue(self, item_id):
        with self.lock:
            it = self._find(item_id)[1]
            if it is not None:
                self.cued = item_id
                self._warm(it)
            return self.cued

    # ---- made before the take: the cued design's layers and their PNGs, so Take is instant ----
    def _key(self, it):
        w, h = self.size
        t = self.s.get("trans") or {}
        return (it["id"], int(it.get("rev") or 0), w, h, self.fps, t.get("type") or "", float(t.get("dur") or 0))

    def _make_anim(self, it, timers=False):
        """A new Animator for it at the output size, with the Live transition and (timers) the clocks now."""
        k = self._key(it)
        it = copy.deepcopy(it)
        if k[5]:                                  # the Live transition: the same for every layer, in and out
            kind = "none" if k[5] == "cut" else k[5]
            for L in (it.get("design") or {}).get("layers") or []:
                L["anim_in"] = {"type": kind, "delay": 0, "dur": k[6]}
                L.pop("anim_out", None)
        if timers:
            self._with_timers(it)
        return design.Animator(it, k[2], k[3], k[4])

    def _animator(self, it):
        """The item's Animator (made once and kept: the cued one is made ahead so Take is instant).
        An item with a running clock is made fresh each time (its text changes)."""
        if self._timer_layers(it):
            return self._make_anim(it, timers=True)
        k = self._key(it)
        a = self._anims.get(k)
        if a is None:
            a = self._anims[k] = self._make_anim(it)
            while len(self._anims) > 6:
                self._anims.pop(next(iter(self._anims)))
        return a

    def _warm(self, it):
        it = copy.deepcopy(it)

        def go():
            try:
                a = self._animator(it)
                for i in range(len(a.layers)):
                    self._png(a, i)
            except Exception:
                logging.exception("cg live: preparing %s", it.get("name"))
        threading.Thread(target=go, daemon=True, name="cg live warm").start()

    @staticmethod
    def _png(anim, i):
        pngs = anim.__dict__.setdefault("pngs", {})
        if i not in pngs:
            buf = io.BytesIO()
            anim.layers[i][1].save(buf, "PNG", compress_level=1)
            pngs[i] = buf.getvalue()
        return pngs[i]

    def take(self, item_id=None):
        """item_id (or the cued one) to Live; what is on air goes out as it comes in."""
        if self.s.get("playout"):
            self.link_playout()
        with self.lock:
            item_id = item_id or self.cued
            _pl, it = self._find(item_id) if item_id else (None, None)
            if it is None:
                return "nothing cued"
            for p in self.progs:
                p.go_out()
            w, h = self.size
            self.progs.append(Program(copy.deepcopy(it), w, h, self.fps, self._animator(it)))
            self.progs = self.progs[-4:]
            self.live_item = item_id
            self._atem(True)
            self.s["was_live"] = item_id
            self._save()
            self.version += 1
            nxt = self._step(item_id, 1)          # the next one is cued, ready for the next take
            if nxt:
                self.cue(nxt)
            self.cv.notify_all()
            return ""

    def clear(self):
        with self.lock:
            for p in self.progs:
                p.go_out()
            if self.live_item:
                self._atem(False)
            self.live_item = None
            self.s.pop("was_live", None)
            self._save()
            self.version += 1
            self.cv.notify_all()

    def _step(self, item_id, d):
        pl = self._pl()
        ids = [it["id"] for it in pl["items"]]
        if not ids:
            return None
        if item_id not in ids:
            return ids[0] if d > 0 else ids[-1]
        i = ids.index(item_id) + d
        return ids[i] if 0 <= i < len(ids) else None

    def next(self, take=True):
        """take: the cued one if it is not on air yet, else the one after what is on air. Not take: cue the next."""
        with self.lock:
            if take:
                target = self.cued if self.cued and self.cued != self.live_item else self._step(self.live_item or self.cued, 1)
            else:
                target = self._step(self.cued or self.live_item, 1)
            if target is None:
                return "end of the playlist"
            return self.take(target) if take else (self.cue(target) and "")

    def prev(self, take=True):
        with self.lock:
            target = self._step((self.live_item or self.cued) if take else (self.cued or self.live_item), -1)
            if target is None:
                return "start of the playlist"
            return self.take(target) if take else (self.cue(target) and "")

    def by_number(self, n, take=True):
        with self.lock:
            items = self._pl()["items"]
            if not 1 <= n <= len(items):
                return f"no item {n} in {self._pl()['name']}"
            iid = items[n - 1]["id"]
            return self.take(iid) if take else (self.cue(iid) and "")

    def set_field(self, name, value, item_id=None, n=None):
        """A field of the item on air (or item_id / number n in the playlist): scores from a Stream Deck."""
        with self.lock:
            if n:
                items = self._pl()["items"]
                item_id = items[n - 1]["id"] if 1 <= n <= len(items) else None
            item_id = item_id or self.live_item or self.cued
            _pl, it = self._find(item_id) if item_id else (None, None)
            if it is None:
                return "no item"
            fields = dict(it.get("fields") or {})
            fields[name] = value
            return self.item("update", item_id, {"fields": fields})

    # ---- countdown / count-up timers -------------------------------------
    # A text layer with "timer": {"mode": "down" | "up", "secs": 300, "format": "mm:ss"} shows a running clock.
    # Each item keeps its clock in item["timer"] = {"running", "elapsed" (s before the last start), "t0" (wall)}.
    @staticmethod
    def _timer_layers(it):
        return [L for L in (it.get("design") or {}).get("layers") or [] if L.get("type") == "text" and L.get("timer")]

    @staticmethod
    def _elapsed(it):
        t = it.get("timer") or {}
        e = float(t.get("elapsed") or 0)
        if t.get("running"):
            e += time.time() - float(t.get("t0") or time.time())
        return max(0.0, e)

    @staticmethod
    def clock_text(secs, fmt="mm:ss"):
        secs = int(secs + 0.999) if secs > 0 else 0
        h, m, s = secs // 3600, secs // 60 % 60, secs % 60
        if fmt == "hh:mm:ss" or h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        if fmt == "ss":
            return str(secs)
        return f"{secs // 60:02d}:{s:02d}"

    def _with_timers(self, it):
        """it with each timer layer's text set to the clock now (the item given is changed)."""
        e = self._elapsed(it)
        for L in self._timer_layers(it):
            tm = L["timer"]
            total = float(tm.get("secs") or 0)
            v = max(0.0, total - e) if tm.get("mode", "down") == "down" else e
            L["text"] = (tm.get("prefix") or "") + self.clock_text(v, tm.get("format") or "mm:ss")
            L.pop("field", None)
        return it

    def timer(self, op, item_id=None, secs=None):
        """start / stop / toggle / reset (secs: a new length for countdowns) the timer of an item (or the one on air)."""
        with self.lock:
            item_id = item_id or self.live_item or self.cued
            _pl, it = self._find(item_id) if item_id else (None, None)
            if it is None or not self._timer_layers(it):
                return "no timer"
            t = it.setdefault("timer", {"running": False, "elapsed": 0.0})
            if op == "toggle":
                op = "stop" if t.get("running") else "start"
            if op == "start" and not t.get("running"):
                t.update(running=True, t0=time.time())
            elif op == "stop" and t.get("running"):
                t.update(running=False, elapsed=self._elapsed(it))
            elif op == "reset":
                t.update(running=False, elapsed=0.0)
                if secs is not None:
                    for L in self._timer_layers(it):
                        L["timer"]["secs"] = max(0, float(secs))
            self._save()
            if item_id == self.live_item:
                self._update_on_air(it)
            return ""

    def _tick(self):
        """Every quarter second: timers on air move on, data links bring new values."""
        last = {}
        while not self.stop:
            time.sleep(0.25)
            try:
                with self.lock:
                    it = self._find(self.live_item)[1] if self.live_item else None
                    if it is not None and self._timer_layers(it):
                        txt = [L.get("text") for L in self._timer_layers(self._with_timers(copy.deepcopy(it)))]
                        if txt != last.get(it["id"]):
                            last[it["id"]] = txt
                            self._update_on_air(it)
                        t = it.get("timer") or {}
                        if t.get("running") and all(L["timer"].get("mode", "down") == "down" for L in self._timer_layers(it)) \
                                and self._elapsed(it) >= max(float(L["timer"].get("secs") or 0) for L in self._timer_layers(it)):
                            t.update(running=False, elapsed=self._elapsed(it))      # reached 0:00
                            self._save()
                self._poll_data()
            except Exception:
                logging.exception("cg live tick")

    # ---- data links: fields filled from a JSON address (scores) ------------
    # item["data"] = {"url": "http://...", "every": 5, "map": {"score1": "home.score", ...}}
    @staticmethod
    def _path(obj, path):
        for part in str(path).split("."):
            if isinstance(obj, list):
                try:
                    obj = obj[int(part)]
                except (ValueError, IndexError):
                    return None
            elif isinstance(obj, dict):
                obj = obj.get(part)
            else:
                return None
        return obj

    def _poll_data(self):
        now = time.time()
        with self.lock:
            due = [it for p in self.s["playlists"] for it in p["items"]
                   if (it.get("data") or {}).get("url") and (it.get("data") or {}).get("map")
                   and now - self._polled.get(it["id"], 0) >= max(1.0, float(it["data"].get("every") or 5))
                   and it["id"] in (self.live_item, self.cued)]
            for it in due:
                self._polled[it["id"]] = now
        for it in due:
            threading.Thread(target=self._fetch_data, args=(it["id"], dict(it["data"])), daemon=True,
                             name="cg live data").start()

    def _fetch_data(self, item_id, data):
        import urllib.request
        try:
            req = urllib.request.Request(data["url"], headers={"User-Agent": "Endora CG Composer"})
            with urllib.request.urlopen(req, timeout=5) as r:
                j = json.loads(r.read())
        except Exception as e:
            self.data_errors[item_id] = str(e)[:160]
            return
        self.data_errors.pop(item_id, None)
        with self.lock:
            _pl, it = self._find(item_id)
            if it is None:
                return
            fields = dict(it.get("fields") or {})
            for f, path in (data.get("map") or {}).items():
                v = self._path(j, path) if path else None
                if v is not None:
                    fields[f] = str(v)
            if fields != (it.get("fields") or {}):
                self.item("update", item_id, {"fields": fields})

    def set_data(self, item_id, data):
        with self.lock:
            _pl, it = self._find(item_id)
            if it is None:
                return "item not found"
            data = data or {}
            if data.get("url"):
                it["data"] = {"url": str(data["url"]).strip(), "every": max(1.0, float(data.get("every") or 5)),
                              "map": {str(k): str(v).strip() for k, v in (data.get("map") or {}).items() if str(v).strip()}}
            else:
                it.pop("data", None)
            self._polled.pop(item_id, None)
            self._save()
            return ""

    # ---- ATEM: Take / Clear also put a downstream key on / off air -----------
    # Through the ATEM Switcher app on this PC (it keeps the ATEM connection); Composer's full-screen or SDI output
    # goes into the ATEM as the key's fill / key.
    def _atem(self, on):
        a = self.s.get("atem") or {}
        if not a.get("on"):
            return

        def go():
            try:
                import websocket
                ws = websocket.create_connection(f"ws://127.0.0.1:{int(a.get('port') or 3000)}/", timeout=3,
                                                 header=["User-Agent: Endora CG Composer"])
                ws.send(json.dumps({"cmd": "hello", "role": "controller"}))
                key = max(0, int(a.get("key") or 0))
                ws.send(json.dumps({"cmd": "dsk", "key": key, "onAir": bool(on)}))
                time.sleep(0.2)
                ws.close()
                self.atem_msg = f"DSK {key + 1} {'on' if on else 'off'} air"
            except Exception as e:
                self.atem_msg = f"ATEM Switcher not reachable: {e}"[:160]
        threading.Thread(target=go, daemon=True, name="cg live atem").start()

    def set_atem(self, on=None, key=None):
        with self.lock:
            a = self.s.setdefault("atem", {"on": False, "key": 0})
            if on is not None:
                a["on"] = bool(on)
            if key is not None:
                a["key"] = max(0, min(3, int(key)))
            self._save()

    # ---- the picture ---------------------------------------------------
    def overlay_state(self):
        """For the overlay page: the designs showing, their layer pictures, each layer's animations and how
        long each design has been in its phase. The page moves the layers itself (smooth at the screen's rate)."""
        now = time.monotonic()
        with self.lock:
            for p in self.progs:
                p.states(now)                    # moves phases on (in -> on, out -> gone)
            self.progs = [p for p in self.progs if p.phase != "gone"]
            out = [{"id": p.id, "phase": p.phase, "t": round(now - p.t0, 3),
                    "pics": [[x, y, im.width, im.height] for _L, im, x, y in p.anim.layers], "r": list(p.revs),
                    "in": [list(a) for a in p.anim.anims["in"]], "out": [list(a) for a in p.anim.anims["out"]]}
                   for p in self.progs]
            w, h = self.size
            return {"v": self.version, "w": w, "h": h, "progs": out}

    def layer_png(self, pid, i):
        with self.lock:
            p = next((p for p in self.progs if p.id == pid), None)
        if p is None or not 0 <= i < len(p.anim.layers):
            return None
        return self._png(p.anim, i)

    def picture(self):
        """The Live output as an RGBA PIL picture."""
        from PIL import Image
        now = time.monotonic()
        with self.lock:
            progs = [p for p in self.progs if p.phase != "gone"]
            states = [(p, p.states(now)) for p in progs]
        w, h = self.size
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for p, st in states:
            if st:
                canvas.alpha_composite(p.anim.compose(st))
        return canvas

    def wait_change(self, version, timeout):
        with self.cv:
            if self.version == version:
                self.cv.wait(timeout)
            return self.version

    # ---- outputs -------------------------------------------------------
    def transition(self, kind=None, dur=None):
        with self.lock:
            t = self.s["trans"]
            if kind is not None and kind in ("", "cut", "fade", "dwipe-l", "dwipe-r", "wipe-l", "slide-l", "slide-u", "scale"):
                t["type"] = kind
            if dur is not None:
                t["dur"] = max(0.0, min(5.0, float(dur)))
            self._save()
            if self.cued:
                it = self._find(self.cued)[1]
                if it is not None:
                    self._warm(it)

    def outputs(self, res=None, fps=None, ndi_on=None, ndi_name=None, playout=None):
        with self.lock:
            if playout is not None:
                self.s["playout"] = bool(playout)
                if playout:
                    ndi_on = True
            if res in RESOLUTIONS:
                self.s["res"] = res
            if fps and int(fps) in FPS:
                self.s["fps"] = int(fps)
            if ndi_on is not None:
                self.s["ndi"]["on"] = bool(ndi_on)
            if ndi_name is not None and str(ndi_name).strip():
                self.s["ndi"]["name"] = str(ndi_name).strip()[:60]
            self._save()
            # designs on air are made again at the new size
            if res or fps:
                w, h = self.size
                for p in self.progs:
                    ph = p.phase
                    p.anim = design.Animator(p.item, w, h, self.fps)
                    p.phase = ph
                self.version += 1
        self._apply_outputs()

    # ---- Playout on this PC ----------------------------------------------
    def link_playout(self):
        """Our NDI output on, and Playout's NDI key on it (unless the key is busy with another source)."""
        if not self.s["ndi"]["on"]:
            self.outputs(ndi_on=True)
        if self._linking:
            return
        self._linking = True
        threading.Thread(target=self._link, daemon=True, name="cg live playout link").start()

    def _playout(self, path, body=None):
        import urllib.request
        url = f"http://127.0.0.1:{self.playout_port}{path}"
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())

    def _link(self):
        try:
            want = f"({self.s['ndi']['name']})"
            for _ in range(20):                     # a new NDI source takes a moment to be seen
                try:
                    info = self._playout("/api/ndi")
                except Exception:
                    self.link_msg = "Playout is not running on this PC."
                    return
                key = info.get("key") or {}
                mine = sorted((d["name"] for d in info.get("sources") or [] if d["name"].endswith(want)),
                              key=lambda n: not n.upper().startswith(socket.gethostname().upper()))
                if key.get("source") and key["source"] in mine and key.get("on"):
                    self.link_msg = "On Playout's program (NDI key)."
                    return
                if key.get("on") and key.get("source") and key["source"] not in mine:
                    self.link_msg = f"Playout's NDI key is on air with {key['source']}: not changed."
                    return
                if mine:
                    self._playout("/api/ndi/key", {"source": mine[0], "on": True})
                    self.link_msg = "On Playout's program (NDI key)."
                    return
                time.sleep(0.5)
            self.link_msg = "Playout does not see this NDI output yet."
        except Exception as e:
            self.link_msg = f"Could not reach Playout: {e}"[:200]
        finally:
            self._linking = False

    def shutdown(self):
        """Closing: a see-through picture last, so nothing stays frozen on a receiver."""
        self.stop = True
        ndi = self.ndi
        if ndi is not None:
            w, h = self.size
            blank = np.zeros(w * h * 4, np.uint8)
            try:
                for _ in range(3):
                    ndi.send(blank, self._silence)
                    time.sleep(1.0 / self.fps)
                ndi.close()
            except Exception:
                pass

    def _apply_outputs(self):
        want = self.s["ndi"]["on"]
        old, self.ndi = self.ndi, None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self.ndi_error = ""
        if not want:
            return
        try:
            from ndi_io import NDIOutput
            w, h = self.size
            self.ndi = NDIOutput(self.s["ndi"]["name"], w, h, Fraction(self.fps))
            self._silence = np.zeros((2, int(round(48000 / self.fps))), np.float32)
        except Exception as e:
            self.ndi_error = str(e)[:200]
            logging.warning("CG Composer NDI output: %s", e)

    def _run(self):
        """Keeps the NDI output fed at the frame rate: a new picture while something moves, else the last one."""
        last_v, last = -1, None
        nxt = time.monotonic()
        while not self.stop:
            ndi = self.ndi
            period = 1.0 / self.fps
            nxt += period
            now = time.monotonic()
            if nxt < now - 0.5:
                nxt = now
            if ndi is not None:
                with self.lock:
                    moving = any(p.moving for p in self.progs)
                    v = self.version
                if moving or v != last_v or last is None:
                    im = self.picture()           # PIL writes BGRA itself (numpy channel swapping took 20 ms)
                    last = np.frombuffer(bytearray(im.tobytes("raw", "BGRA")), np.uint8)   # flat: cyndilib takes uint8[:]
                    last_v = v
                try:
                    ndi.send(last, self._silence)
                    self._sent += 1
                except Exception as e:
                    self.ndi_error = str(e)[:200]
            time.sleep(max(0.0, nxt - time.monotonic()))

    # ---- packages (.cgpack): playlists with every picture their designs use --------
    def export_package(self, dest, pids=None):
        with self.lock:
            pls = [copy.deepcopy(p) for p in self.s["playlists"] if not pids or p["id"] in pids]
        pics = set()
        for p in pls:
            for it in p["items"]:
                for L in (it.get("design") or {}).get("layers") or []:
                    src = os.path.basename(str(L.get("src") or "")) if L.get("type") == "image" else ""
                    if src and os.path.isfile(os.path.join(self.folder, src)):
                        pics.add(src)
        tmp = dest + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("project.json", json.dumps({"version": 1, "playlists": pls}, ensure_ascii=False, indent=1))
            for n in sorted(pics):
                z.write(os.path.join(self.folder, n), "media/" + n)
        os.replace(tmp, dest)
        return dest

    def import_package(self, src):
        """Adds the package's playlists (their pictures go into the CG folder)."""
        from cgedit import _unpack
        from paths import safe_name
        with zipfile.ZipFile(src) as z:
            meta = json.loads(z.read("project.json"))
            have = set(z.namelist())
            pls = meta.get("playlists") or []
            for p in pls:
                for it in p.get("items") or []:
                    for L in (it.get("design") or {}).get("layers") or []:
                        s = safe_name(os.path.basename(str(L.get("src") or ""))) if L.get("type") == "image" else ""
                        if s and "media/" + s in have:
                            L["src"] = _unpack(z, "media/" + s, s, self.folder)
        with self.lock:
            names = []
            for p in pls:
                p["id"] = _new_id()
                p["name"] = str(p.get("name") or "Imported")
                for it in p.get("items") or []:
                    it["id"] = _new_id()
                p.setdefault("items", [])
                self.s["playlists"].append(p)
                names.append(p["name"])
            if pls:
                self.s["active"] = pls[0]["id"]
            self._save()
        return names
