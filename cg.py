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
"""
import os
import threading
import uuid

import av
import numpy as np
from PIL import Image

from media import ClipPlayer, probe

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


def composite(dst, src, x, y, opacity=255):
    """Alpha-blend src (h,w,4 BGRA) onto dst (H,W,4 BGRA) at x,y, in place, region only."""
    H, W = dst.shape[:2]
    h, w = src.shape[:2]
    dx0, dy0 = max(0, x), max(0, y)
    dx1, dy1 = min(W, x + w), min(H, y + h)
    if dx0 >= dx1 or dy0 >= dy1 or opacity <= 0:
        return
    s = src[dy0 - y:dy1 - y, dx0 - x:dx1 - x]
    d = dst[dy0:dy1, dx0:dx1]
    a = s[..., 3]
    if opacity >= 255 and a.min() == 255:
        d[..., :3] = s[..., :3]          # fully opaque (JPEG): plain copy
        return
    a = a.astype(np.uint16)
    if opacity < 255:
        a = a * opacity // 255
    a3 = a[..., None]
    d[..., :3] = ((s[..., :3] * a3 + d[..., :3] * (255 - a3) + 127) // 255).astype(np.uint8)


class CGLayer:
    def __init__(self, cfg, folder, fps):
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
        self._image = None
        self._player = None
        info = cg_info(self.path) if os.path.isfile(self.path) else {"w": 0, "h": 0}
        zoom = max(1.0, float(cfg.get("zoom") or 100)) / 100
        self.w = max(1, int(round(info["w"] * zoom)))
        self.h = max(1, int(round(info["h"] * zoom)))
        if not os.path.isfile(self.path) or not self.kind:
            self.error = "file missing"
            self.state = "off"
        elif self.kind == "image":
            im = Image.open(self.path).convert("RGBA")
            if (self.w, self.h) != im.size:
                im = im.resize((self.w, self.h), Image.LANCZOS)
            rgba = np.asarray(im)
            self._image = np.ascontiguousarray(rgba[..., [2, 1, 0, 3]])   # RGBA -> BGRA
        else:
            self._player = self._open_video()

    def _open_video(self):
        # ClipPlayer decodes ahead and paces frames to the output frame rate; bgra keeps alpha
        return ClipPlayer(self.path, self.w + (self.w & 1), self.h + (self.h & 1), Fraction_from(self.fps))

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

    def tick(self):
        """Advance one output frame. Returns (bgra image, opacity 0..255) or None."""
        self.ticks += 1
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
        img = self._frame()
        if img is None:
            return None
        opacity = 255 if (not self.fade or self.state == "on") else int(255 * self.level / self.fade)
        return img, opacity

    def _frame(self):
        if self._image is not None:
            return self._image
        p = self._player
        if p is None:
            return None
        if p.finished():
            p.close()
            if self.cfg.get("loop", True):
                self._player = p = self._open_video()
            else:
                self.state = "out" if self.state != "off" else "off"
                return self._last if hasattr(self, "_last") else None
        if not p.ready():
            return getattr(self, "_last", None)
        flat, _ = p.next_frame(0)
        if flat is None:
            return getattr(self, "_last", None)
        self._last = flat.reshape(p.height, p.width, 4)
        return self._last

    def close(self):
        if self._player:
            self._player.close()


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
        self._card_layers = []        # CGLayer for the on-air live card
        self._card_id = None
        self.on_change = None

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
                elif _changed(layer.cfg, cfg) and layer.visible:
                    layer.close()
                    new = CGLayer({**cfg, "fade": 0}, self.folder, self.fps)
                    new.fade = int(cfg.get("fade", 8))
                    new.level = new.fade
                    self._manual_layers[cid] = new
                else:
                    layer.cfg.update({k: cfg.get(k) for k in ("live_only", "name")})
        if self.on_change:
            self.on_change(self.manual)

    def take_in(self, cid):
        with self._lock:
            cfg = next((c for c in self.manual if c["id"] == cid), None)
            if cfg is None:
                return False
            layer = self._manual_layers.get(cid)
            if layer and layer.visible:
                layer.take_in()
                return True
            if layer:
                layer.close()
            self._manual_layers[cid] = CGLayer(cfg, self.folder, self.fps)
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

    # live card CGs
    def start_card(self, item_id, cfgs):
        with self._lock:
            for layer in self._card_layers:
                layer.close()
            self._card_id = item_id
            self._card_layers = [CGLayer(c, self.folder, self.fps) for c in (cfgs or []) if c.get("file")]

    def stop_card(self):
        with self._lock:
            for layer in self._card_layers:
                layer.take_out()
            self._card_id = None

    # rendering (output thread)
    def render(self, frame_flat, is_live):
        with self._lock:
            jobs = []
            for layer in self._card_layers:
                r = layer.tick()
                if r is not None:
                    jobs.append((layer, r))
            for layer in [l for l in self._card_layers if l.state == "off"]:
                layer.close()
            self._card_layers = [l for l in self._card_layers if l.state != "off"]
            for cfg in self.manual:
                layer = self._manual_layers.get(cfg["id"])
                if layer is None:
                    continue
                r = layer.tick()
                if r is None:
                    continue
                if cfg.get("live_only") and not is_live:
                    continue
                jobs.append((layer, r))
        if not jobs:
            return frame_flat
        out = np.array(frame_flat, copy=True).reshape(self.height, self.width, 4)
        for layer, (img, opacity) in jobs:
            composite(out, img, int(layer.cfg.get("x") or 0), int(layer.cfg.get("y") or 0), opacity)
        return out.reshape(-1)

    def status(self):
        with self._lock:
            on = {cid: l.state for cid, l in self._manual_layers.items() if l.visible or l.state == "wait"}
            card = [{"file": l.cfg.get("file"), "state": l.state} for l in self._card_layers]
            return {"manual_on": on, "card": card, "card_id": self._card_id}


def _changed(a, b):
    return any(a.get(k) != b.get(k) for k in ("file", "x", "y", "zoom", "loop"))
