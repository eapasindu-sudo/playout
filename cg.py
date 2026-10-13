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
        self._stinger = None          # CGLayer of a stinger transition in progress (drawn on top)
        self.stinger_audio = None
        self.on_change = None

    def _layer(self, cfg):
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
                elif _changed(layer.cfg, cfg) and layer.visible:
                    layer.close()
                    new = self._layer({**cfg, "fade": 0})
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
            self._manual_layers[cid] = self._layer(cfg)
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

    @property
    def stinger_active(self):
        return self._stinger is not None

    # rendering (output thread)
    def render(self, frame_flat, is_live, n_samples=0):
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
        if not jobs:
            return frame_flat
        out = np.array(frame_flat, copy=True).reshape(self.height, self.width, 4)
        for layer, (img, opacity, (dx, dy)) in jobs:
            composite(out, img, int(layer.cfg.get("x") or 0) + dx, int(layer.cfg.get("y") or 0) + dy, opacity)
        return out.reshape(-1)

    def status(self):
        with self._lock:
            on = {cid: l.state for cid, l in self._manual_layers.items() if l.visible or l.state == "wait"}
            card = [{"file": l.cfg.get("file") or l.cfg.get("name"), "state": l.state} for l in self._card_layers]
            return {"manual_on": on, "card": card, "card_id": self._card_id}


def _changed(a, b):
    return any(a.get(k) != b.get(k) for k in FX_KEYS)


FX_KEYS = ("file", "x", "y", "zoom", "zoom_x", "zoom_y", "rotate", "crop", "loop", "key", "key_color",
           "key_similarity", "key_blend", "key_spill", "luma_threshold", "luma_softness")
