"""CG (character generator) graphics keyed over the program with alpha.

Supported files: PNG (with alpha), JPEG, and QuickTime/MOV (ProRes 4444, QT Animation,
PNG-in-MOV keep their alpha; other codecs show as opaque).

A CG layer config (dict):
    id, file, x, y      top-left corner in output pixels
    zoom                size in % of the file's own size
    loop                (video CG) start again when it ends; otherwise it takes itself out
    fade                fade in/out length in frames
    delay, duration     live-card CGs: seconds after the card starts / seconds on screen (0 = whole card)
    live_only           manual CGs: hide while a commercial is on air, show again on live

A web overlay (kind "web": a scoreboard or other overlay link) is drawn by webcg.WebRenderer.
"""
import os
import threading
import uuid

import av
import numpy as np
from PIL import Image

import gpucg

from cgfx import FxGraph, render_still
from textcg import TextLayer
from media import ClipPlayer, open_video, probe

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".gif", ".webp"}
VIDEO_EXT = {".mov", ".mp4", ".webm", ".avi", ".mkv", ".mxf"}
CG_EXT = IMAGE_EXT | VIDEO_EXT


def cg_kind(name):
    ext = os.path.splitext(name)[1].lower()
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None


_info_cache = {}


def cg_info(path):
    """{w, h, duration, alpha} of a CG file (cached by mtime)."""
    key = (path, os.path.getmtime(path))
    if key in _info_cache:
        return _info_cache[key]
    info = {"w": 0, "h": 0, "duration": None, "alpha": False}
    try:
        if cg_kind(path) == "image":
            with Image.open(path) as im:
                info["w"], info["h"] = im.size
                info["alpha"] = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        else:
            with av.open(path) as c:
                vs = c.streams.video[0]
                info["w"], info["h"] = vs.codec_context.width, vs.codec_context.height
                fmt = vs.codec_context.pix_fmt or ""
                info["alpha"] = any(t in fmt for t in ("yuva", "rgba", "bgra", "argb", "abgr", "ya"))
            info["duration"] = probe(path)
    except Exception:
        pass
    _info_cache[key] = info
    return info


def list_cg(folder):
    out = []
    for name in sorted(os.listdir(folder), key=str.lower):
        p = os.path.join(folder, name)
        if os.path.isfile(p) and cg_kind(name):
            out.append({"name": name, "kind": cg_kind(name), **cg_info(p)})
    return out


def cg_preview(path, cache_dir):
    """PNG (keeps alpha) of the CG's first frame, for the position editor."""
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, f"cg2.{os.path.basename(path)}.{int(os.path.getmtime(path))}.png")
    if os.path.exists(out):
        return out
    if cg_kind(path) == "image":
        Image.open(path).convert("RGBA").save(out)
    else:
        with av.open(path) as c:
            for frame in c.decode(c.streams.video[0]):
                Image.fromarray(frame.to_ndarray(format="rgba"), "RGBA").save(out)   # keep the alpha
                break
    return out


def _rgba(buf):
    """A PIL image drawing straight into a C-contiguous (h, w, 4) uint8 numpy buffer: no copies."""
    h, w = buf.shape[:2]
    im = Image.frombuffer("RGBA", (w, h), buf, "raw", "RGBA", 0, 1)
    im.readonly = 0
    return im


def _inplace_works():
    try:
        buf = np.zeros((2, 2, 4), np.uint8)
        _rgba(buf).paste(Image.new("RGBA", (2, 2), (9, 9, 9, 255)), (0, 0))
        return int(buf[0, 0, 0]) == 9
    except Exception:
        return False


_INPLACE = _inplace_works()   # a Pillow that copies on paste: blend a copy and write it back


def composite(dst, src, x, y, opacity=255):
    """Alpha-blend src (h,w,4 BGRA) onto dst (H,W,4 BGRA) at x,y, in place, region only.
    See-through margins of the CG are skipped, and the blend runs in C on the picture itself."""
    H, W = dst.shape[:2]
    h, w = src.shape[:2]
    dx0, dy0 = max(0, x), max(0, y)
    dx1, dy1 = min(W, x + w), min(H, y + h)
    if dx0 >= dx1 or dy0 >= dy1 or opacity <= 0:
        return
    s = src[dy0 - y:dy1 - y, dx0 - x:dx1 - x]
    a = s[..., 3]
    if a.size > 100_000:                 # a big CG (often a full-frame PNG with a small graphic): only its drawn part
        box = Image.fromarray(np.ascontiguousarray(a)).getbbox()
        if box is None:
            return
        l, t, r, b = box
        s, a = s[t:b, l:r], a[t:b, l:r]
        dx0, dy0, dx1, dy1 = dx0 + l, dy0 + t, dx0 + r, dy0 + b
    d = dst[dy0:dy1, dx0:dx1]
    if opacity >= 255 and a.min() == 255:
        d[..., :3] = s[..., :3]          # fully opaque (JPEG): plain copy
        return
    mask = np.ascontiguousarray(a) if opacity >= 255 else (a.astype(np.uint16) * opacity // 255).astype(np.uint8)
    keep = d[..., 3].copy()              # the program's own alpha stays as it is
    buf = d if d.flags.c_contiguous else np.ascontiguousarray(d)
    if _INPLACE:
        _rgba(buf).paste(Image.fromarray(np.ascontiguousarray(s), "RGBA"), (0, 0), Image.fromarray(mask, "L"))
    else:
        im = Image.fromarray(buf if buf is not d else buf.copy(), "RGBA")
        im.paste(Image.fromarray(np.ascontiguousarray(s), "RGBA"), (0, 0), Image.fromarray(mask, "L"))
        buf = np.asarray(im)
    if buf is not d:
        d[...] = buf
    d[..., 3] = keep


class CGLayer:
    """One CG on air: fades, delay / duration, and the picture with its effects (cgfx)."""

    CACHE_LIMIT = 400 * 1024 * 1024   # keep a looping video CG's keyed frames in memory up to this size

    def __init__(self, cfg, folder, fps, audio=False):
        self.cfg = dict(cfg)
        self.fps = float(fps)
        self.path = os.path.join(folder, cfg.get("file", ""))
        self.kind = cg_kind(self.path)
        self.fade = max(0, int(cfg.get("fade", 8)))
        self.delay_frames = int(float(cfg.get("delay") or 0) * self.fps)
        dur = float(cfg.get("duration") or 0)
        self.duration_frames = int(dur * self.fps) if dur > 0 else None
        self.state = "wait" if self.delay_frames else "in"
        self.level = 0           # current fade 0..fade
        self.ticks = 0
        self.on_ticks = 0
        self.error = ""
        self.want_audio = audio
        self.audio = None
        self._image = None       # (bgra, (dx, dy)) for stills
        self._player = None
        self._last = None
        self._fx = None
        self._cache, self._cache_bytes, self._cache_ok = [], 0, True
        self._replay = None      # index into _cache while replaying a looped CG from memory
        if not os.path.isfile(self.path) or not self.kind:
            self.error = "file missing"
            self.state = "off"
        elif self.kind == "image":
            try:
                img, off = render_still(self.path, "image", self.cfg)
                self._image = (img, off) if img is not None else None
            except Exception as e:
                self.error = str(e)
                self.state = "off"
        else:
            self._player = self._open_video()

    def _process(self, frame):
        if self._fx is None:
            self._fx = FxGraph(self.cfg, frame.width, frame.height, frame.format.name)
        img, off = self._fx.process(frame)
        return (img, off) if img is not None else None

    def _open_video(self):
        # decodes ahead on its own thread; the key / crop / rotate runs there too
        return ClipPlayer(self.path, 2, 2, Fraction_from(self.fps), process=self._process)

    def take_out(self):
        if self.state in ("wait",):
            self.state = "off"
        elif self.state in ("in", "on"):
            self.state = "out"

    def take_in(self):
        if self.state in ("out", "off"):
            self.state = "in"

    @property
    def visible(self):
        return self.state in ("in", "on", "out")

    def tick(self, n_samples=0):
        """Advance one output frame. Returns (bgra image, opacity 0..255, (dx, dy)) or None."""
        self.ticks += 1
        self.audio = None
        if self.state == "off":
            return None
        if self.state == "wait":
            if self.ticks >= self.delay_frames:
                self.state = "in"
            else:
                return None
        if self.state == "in":
            self.level = min(self.fade, self.level + 1) if self.fade else 0
            if not self.fade or self.level >= self.fade:
                self.state = "on"
        elif self.state == "out":
            self.level = max(0, self.level - 1) if self.fade else 0
            if self.level <= 0:
                self.state = "off"
                return None
        if self.state == "on":
            self.on_ticks += 1
            if self.duration_frames and self.on_ticks >= self.duration_frames:
                self.state = "out"
        pic = self._frame(n_samples)
        if pic is None:
            return None
        opacity = 255 if (not self.fade or self.state == "on") else int(255 * self.level / self.fade)
        return pic[0], opacity, pic[1]

    def _frame(self, n):
        if self._image is not None:
            return self._image
        if self._replay is not None:              # looping from memory: no decoding at all
            pic = self._cache[self._replay]
            self._replay = (self._replay + 1) % len(self._cache)
            return pic
        p = self._player
        if p is None:
            return self._last
        if p.finished():
            p.close()
            if self.cfg.get("loop", True) and self.state != "off":
                if self._cache_ok and self._cache:
                    self._player = None
                    self._replay = 0
                    return self._frame(n)
                self._cache, self._cache_ok = [], False
                self._player = p = self._open_video()
            else:
                self._player = None
                if self.state != "off":
                    self.state = "out" if self.fade else "off"
                return self._last
        if not p.ready():
            return self._last
        pic, audio = p.next_frame(n)
        if self.want_audio:
            self.audio = audio
        if pic is not None and pic is not self._last:
            self._last = pic
            if self._cache_ok:
                self._cache_bytes += pic[0].nbytes
                if self._cache_bytes > self.CACHE_LIMIT:
                    self._cache, self._cache_ok = [], False
                else:
                    self._cache.append(pic)
        elif pic is self._last and self._cache_ok and pic is not None:
            self._cache.append(pic)   # same picture held for another output frame
        return self._last

    def close(self):
        if self._player:
            self._player.close()
        self._cache = []


class WebLayer(CGLayer):
    """A web page overlay (cricket scoreboard link...). The page stays loaded while the CG is off air,
    so IN is instant and the score is already current; pictures are only taken while it is on."""

    def __init__(self, cfg, fps, width, height):
        from webcg import WebRenderer
        self.cfg = dict(cfg)
        self.fps = float(fps)
        self.path, self.kind = "", "web"
        self.fade = max(0, int(cfg.get("fade", 8)))
        self.delay_frames = int(float(cfg.get("delay") or 0) * self.fps)
        dur = float(cfg.get("duration") or 0)
        self.duration_frames = int(dur * self.fps) if dur > 0 else None
        self.state = "wait" if self.delay_frames else "in"
        self.level, self.ticks, self.on_ticks = 0, 0, 0
        self.want_audio, self.audio = False, None
        self._image = self._player = self._last = self._fx = self._replay = None
        self._cache, self._cache_bytes, self._cache_ok = [], 0, False
        url = str(cfg.get("url") or "").strip()
        self.r = None
        if not url.lower().startswith(("http://", "https://", "file:")):
            self._error = "Paste the overlay link (it starts with http:// or https://)"
            self.state = "off"
            return
        self._error = ""
        self.r = WebRenderer(url, int(cfg.get("page_w") or 0) or width, int(cfg.get("page_h") or 0) or height,
                             cfg.get("zoom") or 100, cfg.get("rate") or 10, cfg.get("css") or "")

    @property
    def error(self):
        return self._error or (self.r.error if self.r else "")

    @error.setter
    def error(self, v):
        self._error = v

    @property
    def dead(self):
        return self.r is None

    def tick(self, n_samples=0):
        out = super().tick(n_samples)
        if self.r is not None:
            self.r.active = self.state != "off"
        return out

    def _frame(self, n):
        return self.r.frame if self.r else None

    def reload(self):
        if self.r:
            self.r.reload()

    def close(self):
        if self.r:
            self.r.close()


def Fraction_from(fps):
    from fractions import Fraction
    return Fraction(fps).limit_denominator(1001)


class CGManager:
    """Owns the manual CG list and the CGs of the live card on air; renders them over the program."""

    def __init__(self, folder, width, height, fps):
        self.folder = folder
        self.width, self.height = width, height
        self.fps = fps
        self._lock = threading.Lock()
        self.manual = []              # list of cfg dicts (saved)
        self._manual_layers = {}      # id -> CGLayer
        self._warming = set()         # manual CG ids being loaded ahead
        self._card_layers = []        # CGLayer for the on-air live card
        self._card_id = None
        self._stinger = None          # CGLayer of a stinger transition in progress (drawn on top)
        self._squeeze = None          # SqueezeLayer: the program squeezed into an L-ad's green area
        self._comments = []           # TextLayer(s) of viewer comments (the old one fades out as the new comes)
        self.comment_id = None        # id of the comment on air
        self.stinger_audio = None
        self.on_change = None

    def _layer(self, cfg):
        if cfg.get("kind") == "web":
            return WebLayer(cfg, self.fps, self.width, self.height)
        if cfg.get("kind") == "text":
            return TextLayer(cfg, self.width, self.height, self.fps)
        return CGLayer(cfg, self.folder, self.fps)

    # manual CG list
    def set_manual(self, cfgs):
        with self._lock:
            clean = []
            for c in cfgs:
                c = dict(c)
                c["id"] = c.get("id") or uuid.uuid4().hex[:8]
                clean.append(c)
            self.manual = clean
            for cid in list(self._manual_layers):
                layer = self._manual_layers[cid]
                cfg = next((c for c in clean if c["id"] == cid), None)
                if cfg is None:
                    layer.close()
                    del self._manual_layers[cid]
                elif isinstance(layer, TextLayer) and cfg.get("kind") == "text":
                    layer.update(cfg)      # new ticker / name text goes on air at once
                elif isinstance(layer, CGLayer) and not isinstance(layer, WebLayer) and not layer.visible \
                        and _changed(layer.cfg, cfg):
                    layer.close()                  # off air with old settings: loaded again below
                    del self._manual_layers[cid]
                elif isinstance(layer, WebLayer) and _changed(layer.cfg, cfg) and not layer.visible:
                    layer.close()                  # new link / size: a fresh page, loaded off air
                    self._manual_layers[cid] = self._off_layer(cfg)
                elif _changed(layer.cfg, cfg) and layer.visible:
                    layer.close()
                    new = self._layer({**cfg, "fade": 0})
                    new.fade = int(cfg.get("fade", 8))
                    new.level = new.fade
                    self._manual_layers[cid] = new
                else:
                    layer.cfg.update({k: cfg.get(k) for k in ("live_only", "name")})
            for c in clean:                       # web overlays load their page now, ready for IN
                if c.get("kind") == "web" and c["id"] not in self._manual_layers:
                    self._manual_layers[c["id"]] = self._off_layer(c)
        self._warm_all()
        if self.on_change:
            self.on_change(self.manual)

    def _off_layer(self, cfg):
        layer = self._layer(cfg)
        layer.state = "off"
        layer.warm = True
        return layer

    # Manual picture / video CGs are loaded ahead, off air (picture rendered, video decoder open with its
    # first frames ready), so IN puts them on at once instead of opening the file on the output frame.
    def _warm_all(self):
        with self._lock:
            todo = [c for c in self.manual if c.get("kind") not in ("text", "web") and c.get("file")
                    and c["id"] not in self._manual_layers and c["id"] not in self._warming]
            self._warming.update(c["id"] for c in todo)
        if todo:
            threading.Thread(target=self._warm, args=(todo,), daemon=True, name="cg warm").start()

    def _warm(self, cfgs):
        for cfg in cfgs:
            try:
                layer = self._off_layer(cfg)          # built off the output thread
            except Exception:
                layer = None
            with self._lock:
                self._warming.discard(cfg["id"])
                cur = next((c for c in self.manual if c["id"] == cfg["id"]), None)
                if layer is None:
                    continue
                if cur is None or cfg["id"] in self._manual_layers or _changed(cur, cfg) or layer.error:
                    layer.close()
                    continue
                self._manual_layers[cfg["id"]] = layer

    def reload(self, cid):
        with self._lock:
            layer = self._manual_layers.get(cid)
            if isinstance(layer, WebLayer):
                layer.reload()
                return True
        return False

    def take_in(self, cid):
        with self._lock:
            cfg = next((c for c in self.manual if c["id"] == cid), None)
            if cfg is None:
                return False
            layer = self._manual_layers.get(cid)
            if layer and (layer.visible or getattr(layer, "warm", False) or isinstance(layer, WebLayer) and not layer.dead):
                layer.warm = False
                layer.take_in()
                return True
            if layer:
                layer.close()
            layer = self._layer(cfg)
            self._manual_layers[cid] = layer
            if getattr(layer, "error", ""):
                if layer.error == "file missing":
                    return f"{cfg.get('file')} is not in the CG folder any more: open Edit and pick the file again"
                return layer.error
            return True

    def take_out(self, cid):
        with self._lock:
            layer = self._manual_layers.get(cid)
            if layer:
                layer.take_out()

    def all_out(self):
        with self._lock:
            for layer in self._manual_layers.values():
                layer.take_out()
            for layer in self._card_layers:
                layer.take_out()
            if self._squeeze is not None:
                self._squeeze.cancel()
            for layer in self._comments:
                layer.take_out()
            self.comment_id = None

    # live card CGs
    def start_card(self, item_id, cfgs):
        with self._lock:
            for layer in self._card_layers:
                layer.close()
            self._card_id = item_id
            self._card_layers = [self._layer(c) for c in (cfgs or []) if c.get("file") or c.get("kind") == "text"]

    def stop_card(self):
        with self._lock:
            for layer in self._card_layers:
                layer.take_out()
            self._card_id = None

    # stinger transitions
    def start_stinger(self, file):
        """Full-screen video with alpha played over everything (with its sound)."""
        path = os.path.join(self.folder, file or "")
        if not os.path.isfile(path) or cg_kind(path) != "video":
            return False
        info = cg_info(path)
        w, h = info["w"] or self.width, info["h"] or self.height
        zoom = 100.0 * min(self.width / w, self.height / h)
        cfg = {"file": file, "x": int((self.width - w * zoom / 100) / 2), "y": int((self.height - h * zoom / 100) / 2),
               "zoom": zoom, "fade": 0, "loop": False}
        layer = CGLayer(cfg, self.folder, self.fps, audio=True)
        with self._lock:
            if self._stinger:
                self._stinger.close()
            self._stinger = layer
        return True

    # squeeze feed (L-ad)
    def squeeze_in(self, cfg, delay=0.0, duration=0.0):
        from squeeze import SqueezeLayer
        layer = SqueezeLayer(cfg, self.folder, self.fps, self.width, self.height, delay, duration)
        if layer.error:
            layer.close()
            return layer.error
        with self._lock:
            old = self._squeeze
            if old is not None and old.visible and old.name == layer.name and not layer.wait:
                # the same squeeze is already on: keep its overlay running, take the new timing
                layer.close()
                old.take_in()
                old.on_frames, old.on_count, old.owner, old.next = layer.on_frames, 0, None, None
                return ""
            if old is not None and old.visible:
                if layer.wait:
                    old.take_out()                   # this one goes in after the delay, once the old one is out
                    if old.next is not None:
                        old.next.close()
                    old.next = layer
                    return ""
                # no delay: switch straight to the new one at the same point
                layer.pos, layer.state = old.pos, "in"
            if old is not None:
                old.close()
            self._squeeze = layer
        return ""

    def squeeze_update(self, name, changes):
        """New feed position / time / loop for the squeeze on air (moves at once)."""
        from squeeze import clean_box
        with self._lock:
            sq = self._squeeze
            if sq is None or sq.name != name:
                return False
            if changes.get("squeeze"):
                sq.box = clean_box(changes["squeeze"], self.width, self.height) or sq.box
            if changes.get("squeeze_time") is not None:
                old = sq.frames
                sq.frames = max(1, int(float(changes["squeeze_time"]) * float(self.fps)))
                sq.pos = sq.frames if sq.state == "on" else min(sq.frames, sq.pos * sq.frames // old)
            if changes.get("loop") is not None:
                sq.overlay.cfg["loop"] = bool(changes["loop"])
            return True

    def squeeze_preset(self, preset):
        """Squeeze settings saved in a .cgp in the CG folder, or None."""
        from cgedit import list_presets
        for p in list_presets(self.folder):
            if p["name"] == preset and (p.get("cfg") or {}).get("squeeze"):
                return {**p["cfg"], "file": p["file"], "preset": p["name"],
                        "name": p["cfg"].get("name") or os.path.splitext(p["name"])[0]}
        return None

    def squeeze_item(self, item_id, preset, at=0, dur=0):
        """Squeeze attached to a playlist item: IN after `at` seconds, OUT after `dur` (0 = item end)."""
        c = self.squeeze_preset(preset)
        if c is None:
            return f"{preset} not found"
        err = self.squeeze_in(c, delay=float(at or 0), duration=float(dur or 0))
        if not err:
            with self._lock:
                sq = self._squeeze
                if sq is not None:
                    (sq.next or sq).owner = item_id
        return err

    def squeeze_item_end(self, item_id):
        with self._lock:
            sq = self._squeeze
            if sq is not None and sq.next is not None and sq.next.owner == item_id:
                sq.next.close()
                sq.next = None
            elif sq is not None and sq.owner == item_id:
                if sq.state == "wait":
                    sq.close()
                    self._squeeze = None
                else:
                    sq.take_out()

    def squeeze_out(self):
        with self._lock:
            if self._squeeze is not None:
                self._squeeze.cancel()

    @property
    def squeeze_state(self):
        sq = self._squeeze
        return {"name": sq.name, "state": sq.state} if sq is not None and sq.visible else None

    # viewer comments (comments.py)
    def show_comment(self, cfg, cid):
        layer = TextLayer({**cfg, "kind": "text", "style": "comment"}, self.width, self.height, self.fps)
        if layer.error:
            return layer.error
        img = layer._img
        if img is not None:                  # a long comment is taller: keep it on the screen
            h, w = img.shape[:2]
            layer.cfg["y"] = max(0, min(int(layer.cfg.get("y") or 0), self.height - h - self.height // 30))
            layer.cfg["x"] = max(0, min(int(layer.cfg.get("x") or 0), self.width - w))
        with self._lock:
            for old in self._comments:
                old.take_out()
            self._comments.append(layer)
            self.comment_id = cid
        return ""

    def hide_comment(self):
        with self._lock:
            for old in self._comments:
                old.take_out()
            self.comment_id = None

    @property
    def comment_on(self):
        with self._lock:
            live = [l for l in self._comments if l.state in ("wait", "in", "on")]
            return self.comment_id if live else None

    @property
    def stinger_active(self):
        return self._stinger is not None

    # rendering (output thread)
    def render(self, frame_flat, is_live, n_samples=0, collect=False):
        """The program with the CGs on air over it; collect=True: (program, [(image, x, y, opacity)]) for the
        GPU engine to mix instead."""
        with self._lock:
            base = None
            sq = self._squeeze
            if sq is not None:
                base = sq.render(np.asarray(frame_flat).reshape(self.height, self.width, 4))
                if sq.state == "off":
                    nxt, sq.next = sq.next, None
                    sq.close()
                    self._squeeze = nxt              # a squeeze waiting for this one to go out
                    base = None
            jobs = []
            for layer in self._card_layers:
                r = layer.tick()
                if r is not None:
                    jobs.append((layer, r))
            for layer in [l for l in self._card_layers if l.state == "off"]:
                layer.close()
            self._card_layers = [l for l in self._card_layers if l.state != "off"]
            for layer in self._comments:
                r = layer.tick()
                if r is not None:
                    jobs.append((layer, r))
            for layer in [l for l in self._comments if l.state == "off"]:
                layer.close()
            self._comments = [l for l in self._comments if l.state != "off"]
            rewarm = False
            for cfg in self.manual:
                layer = self._manual_layers.get(cfg["id"])
                if layer is None:
                    continue
                if layer.state == "off" and not getattr(layer, "warm", False) and type(layer) is CGLayer:
                    layer.close()                 # went off air: load it again, ready for the next IN
                    del self._manual_layers[cfg["id"]]
                    rewarm = True
                    continue
                r = layer.tick()
                if r is None:
                    continue
                if cfg.get("live_only") and not is_live:
                    continue
                jobs.append((layer, r))
            self.stinger_audio = None
            st = self._stinger
            if st is not None:
                r = st.tick(n_samples)
                self.stinger_audio = st.audio
                if r is not None:
                    jobs.append((st, r))
                if st.state == "off":
                    st.close()
                    self._stinger = None
        if rewarm:
            self._warm_all()
        src = np.asarray(frame_flat).reshape(self.height, self.width, 4) if base is None else base
        layers = [(img, int(layer.cfg.get("x") or 0) + dx, int(layer.cfg.get("y") or 0) + dy, opacity)
                  for layer, (img, opacity, (dx, dy)) in jobs]
        if collect:
            return src, layers
        return self.composite(src, layers, copy=base is None)

    def composite(self, src, layers, copy=True):
        """Mixes the CG pictures over src on this PC (OpenGL, else the CPU). Pages drawn on the GPU engine's
        own GPU (gpuengine.WebTex) are left out: only the engine can draw them."""
        import gpuengine
        layers = [L for L in layers if not isinstance(L[0], gpuengine.WebTex)]
        if not layers:
            return np.asarray(src).reshape(-1)
        gl = gpucg.compositor()
        if gl is not None:
            try:
                return gl.render(src, layers).reshape(-1)
            except Exception as e:
                gpucg.failed(e)
        out = np.array(src, copy=True) if copy else src
        for img, x, y, opacity in layers:
            composite(out, img, x, y, opacity)
        return out.reshape(-1)

    def status(self):
        with self._lock:
            on = {cid: l.state for cid, l in self._manual_layers.items() if l.visible or l.state == "wait"}
            errors = {cid: l.error for cid, l in self._manual_layers.items() if isinstance(l, WebLayer) and l.error}
            card = [{"file": l.cfg.get("file") or l.cfg.get("name"), "state": l.state} for l in self._card_layers]
            return {"manual_on": on, "manual_errors": errors, "card": card, "card_id": self._card_id, "squeeze": self.squeeze_state}


def _changed(a, b):
    return any(a.get(k) != b.get(k) for k in FX_KEYS)


FX_KEYS = ("file", "url", "page_w", "page_h", "rate", "css", "x", "y", "zoom", "zoom_x", "zoom_y", "rotate", "crop", "loop", "key", "key_color",
           "key_similarity", "key_blend", "key_spill", "luma_threshold", "luma_softness")
