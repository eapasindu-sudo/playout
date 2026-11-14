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

    def __init__(self, item, w, h, fps):
        self.item = item
        self.id = _new_id()                  # the overlay page knows a design by this
        self.anim = design.Animator(item, w, h, fps)
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
    def __init__(self, home, folder):
        self.file = os.path.join(home, "composer-live.json")
        self.folder = folder                 # the CG folder (pictures, the CG library)
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.s = self._load()
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
                    self._refresh_live(it)
            self._save()
            return ""

    def _refresh_live(self, it):
        """New text on air at once (no animation): the design is made again and stays on."""
        w, h = self.size
        p = Program(it, w, h, self.fps)
        p.phase = "on"
        self.progs = [p]
        self.version += 1
        self.cv.notify_all()

    # ---- on air --------------------------------------------------------
    def cue(self, item_id):
        with self.lock:
            if self._find(item_id)[1] is not None:
                self.cued = item_id
            return self.cued

    def take(self, item_id=None):
        """item_id (or the cued one) to Live; what is on air goes out as it comes in."""
        with self.lock:
            item_id = item_id or self.cued
            _pl, it = self._find(item_id) if item_id else (None, None)
            if it is None:
                return "nothing cued"
            for p in self.progs:
                p.go_out()
            w, h = self.size
            self.progs.append(Program(copy.deepcopy(it), w, h, self.fps))
            self.progs = self.progs[-4:]
            self.live_item = item_id
            self.s["was_live"] = item_id
            self._save()
            self.version += 1
            nxt = self._step(item_id, 1)          # the next one is cued, ready for the next take
            if nxt:
                self.cued = nxt
            self.cv.notify_all()
            return ""

    def clear(self):
        with self.lock:
            for p in self.progs:
                p.go_out()
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
                    "pics": [[x, y, im.width, im.height] for _L, im, x, y in p.anim.layers],
                    "in": [list(a) for a in p.anim.anims["in"]], "out": [list(a) for a in p.anim.anims["out"]]}
                   for p in self.progs]
            w, h = self.size
            return {"v": self.version, "w": w, "h": h, "progs": out}

    def layer_png(self, pid, i):
        with self.lock:
            p = next((p for p in self.progs if p.id == pid), None)
        if p is None or not 0 <= i < len(p.anim.layers):
            return None
        buf = io.BytesIO()
        p.anim.layers[i][1].save(buf, "PNG", compress_level=1)
        return buf.getvalue()

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
    def outputs(self, res=None, fps=None, ndi_on=None, ndi_name=None):
        with self.lock:
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
