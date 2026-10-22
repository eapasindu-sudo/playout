"""Squeeze feed (L-ad / squeezeback): the program shrinks into the green area of an overlay video.

A squeeze is a CG Composer preset (.cgp) with a "squeeze" box {x, y, w, h} in output pixels. When it
goes IN, the program picture (live feed, clip, whatever is on air) slides from full screen into that
box and the overlay fades in around it; OUT reverses it. With a colour key set on the overlay, the
green is keyed out and the feed shows through it (things drawn over the green stay on top);
without a key the feed is simply drawn over the box.
"""
import av
import numpy as np
from PIL import Image

from cg import CGLayer, composite


def find_green(img):
    """Box {x, y, w, h} of the green screen area in a BGRA / BGR picture, or None."""
    b = img[..., 0].astype(np.int16)
    g = img[..., 1].astype(np.int16)
    r = img[..., 2].astype(np.int16)
    mask = (g > 120) & (g - r > 60) & (g - b > 60)
    if mask.sum() < 400:
        return None
    rows = mask.sum(axis=1)
    cols = mask.sum(axis=0)
    ys = np.where(rows > rows.max() * 0.4)[0]
    xs = np.where(cols > cols.max() * 0.4)[0]
    if not len(ys) or not len(xs):
        return None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def clean_box(box, W, H):
    try:
        x, y = int(float(box.get("x", 0))), int(float(box.get("y", 0)))
        w, h = int(float(box.get("w", W))), int(float(box.get("h", H)))
    except (TypeError, ValueError, AttributeError):
        return None
    w, h = max(16, min(W * 2, w)), max(16, min(H * 2, h))
    return {"x": x, "y": y, "w": w, "h": h}


def place_feed(canvas, feed, box):
    """Scale the program picture (H, W, 4) into box on canvas (in place)."""
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    H, W = canvas.shape[:2]
    if w <= 0 or h <= 0:
        return
    small = _scaled(feed, w, h)
    dx0, dy0, dx1, dy1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if dx0 < dx1 and dy0 < dy1:
        canvas[dy0:dy1, dx0:dx1] = small[dy0 - y:dy1 - y, dx0 - x:dx1 - x]


def _scaled(feed, w, h):
    if (w, h) == (feed.shape[1], feed.shape[0]):
        return np.ascontiguousarray(feed)
    try:                              # FFmpeg's scaler: 2-3x faster than PIL
        return av.VideoFrame.from_ndarray(np.ascontiguousarray(feed), format="bgra").reformat(
            w, h, "bgra", interpolation="BILINEAR").to_ndarray()
    except Exception:
        return np.asarray(_img(feed).resize((w, h), Image.BILINEAR))


def _img(a):
    a = np.ascontiguousarray(a)
    return Image.frombuffer("RGBA", (a.shape[1], a.shape[0]), a, "raw", "RGBA", 0, 1)


def compose(feed, overlay, ox, oy, box, keyed, opacity=255):
    """One squeezed picture: black, the feed in box, the overlay (keyed: over the feed; not keyed: under it)."""
    H, W = feed.shape[:2]
    if keyed and overlay is not None and ox >= 0 and oy >= 0:
        # fast path, all in PIL (C): black canvas, feed pasted in the box, overlay blended on top
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        if box["w"] > 0 and box["h"] > 0:
            canvas.paste(_img(_scaled(feed, box["w"], box["h"])), (box["x"], box["y"]))
        ov = overlay
        if opacity < 255:
            ov = overlay.copy()
            ov[..., 3] = (ov[..., 3].astype(np.uint16) * opacity // 255).astype(np.uint8)
        oh, ow = ov.shape[:2]
        if ox < W and oy < H:
            ov = ov[:H - oy, :W - ox]
            canvas.alpha_composite(_img(ov), (ox, oy))
        return np.array(canvas)
    out = np.zeros((H, W, 4), np.uint8)
    out[..., 3] = 255
    if keyed:
        place_feed(out, feed, box)
        if overlay is not None:
            composite(out, overlay, ox, oy, opacity)
    else:
        if overlay is not None:
            composite(out, overlay, ox, oy, opacity)
        place_feed(out, feed, box)
    return out


class SqueezeLayer:
    """The squeeze on air: overlay video + the program moving into / out of the box."""

    def __init__(self, cfg, folder, fps, width, height, delay=0.0, duration=0.0):
        self.cfg = dict(cfg)
        self.name = cfg.get("name") or cfg.get("file")
        self.width, self.height = width, height
        self.box = clean_box(cfg.get("squeeze") or {}, width, height) or {"x": 0, "y": 0, "w": width, "h": height}
        self.keyed = (cfg.get("key") or "none") != "none"
        st = cfg.get("squeeze_time")
        self.frames = max(1, int(float(0.6 if st is None else st) * float(fps)))
        self.overlay = CGLayer({**self.cfg, "fade": 0, "delay": 0, "duration": 0}, folder, fps)
        self.error = self.overlay.error
        self.wait = int(float(delay or 0) * float(fps))         # frames before it goes in
        self.on_frames = int(float(duration or 0) * float(fps))  # frames squeezed, then out (0 = until OUT)
        self.on_count = 0
        self.next = None             # squeeze to go in once this one is out
        self.owner = None            # playlist item that put it on air (it goes out when that item ends)
        self.state = "wait" if self.wait > 0 else "in"
        self.pos = 0                 # 0 = full screen program .. frames = squeezed
        self._pic = None

    def take_out(self):
        if self.state in ("in", "on"):
            self.state = "out"

    def cancel(self):
        """OUT button: back to full screen, and nothing queued goes in after."""
        if self.next is not None:
            self.next.close()
            self.next = None
        if self.state == "wait":
            self.state = "off"
        self.take_out()

    def take_in(self):
        if self.state in ("out", "off", "wait"):
            self.state = "in"

    @property
    def visible(self):
        return self.state not in ("off", "wait")

    def render(self, feed):
        """feed: program picture (H, W, 4). Returns the squeezed picture, or feed when off."""
        plan = self.plan()
        if plan is None:
            return feed
        box, ov, ox, oy, keyed, opacity = plan
        return compose(feed, ov, ox, oy, box, keyed, opacity)

    def plan(self):
        """Advance one frame: None (the program as it is) or (box, overlay, ox, oy, keyed, opacity), the parts the
        squeezed picture is made of (render() makes it here; the GPU engine makes it on the graphics card)."""
        if self.state == "off":
            return None
        if self.state == "wait":
            self.wait -= 1
            if self.wait > 0:
                return None
            self.state = "in"
        if self.state == "on" and self.on_frames:
            self.on_count += 1
            if self.on_count >= self.on_frames:
                self.state = "out"
        if self.state == "in":
            self.pos += 1
            if self.pos >= self.frames:
                self.pos, self.state = self.frames, "on"
        elif self.state == "out":
            self.pos -= 1
            if self.pos <= 0:
                self.pos, self.state = 0, "off"
                self.overlay.close()
                return None
        r = self.overlay.tick()
        if r is not None:
            self._pic = r
        elif self.overlay.state == "off" and self.state in ("in", "on"):
            self.state = "out"               # overlay played once and ended: back to full screen
        p = self.pos / self.frames
        p = p * p * (3 - 2 * p)               # smooth start and end
        b, W, H = self.box, self.width, self.height
        box = {"x": round(b["x"] * p), "y": round(b["y"] * p),
               "w": round(W + (b["w"] - W) * p), "h": round(H + (b["h"] - H) * p)}
        if self._pic is None:
            ov, ox, oy = None, 0, 0
        else:
            ov = self._pic[0]
            ox = int(self.cfg.get("x") or 0) + self._pic[2][0]
            oy = int(self.cfg.get("y") or 0) + self._pic[2][1]
        return box, ov, ox, oy, self.keyed, int(255 * p)

    def close(self):
        self.overlay.close()
        if self.next is not None:
            self.next.close()
            self.next = None
