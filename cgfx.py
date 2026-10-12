"""CG effects: crop, zoom, colour key (green / blue screen), luma key and rotation.

Runs as an FFmpeg filter graph (fast, multi-threaded C code), so a 1080p green-screen
video CG keys in real time. Used by the playout engine, the CG editor preview and CG Composer.

Effect settings live in the CG config dict next to x / y / zoom:
    zoom_x, zoom_y      extra horizontal / vertical stretch (1 = none)
    rotate              degrees, clockwise
    crop                [x1, y1, x2, y2] in the file's own pixels (empty = whole picture)
    key                 "none" | "chroma" | "luma"
    key_color           "#00ff00"
    key_similarity      0-100   how far from the key colour still counts as background
    key_blend           0-100   soft edge
    key_spill           0-100   removes green / blue fringe from the edges
    luma_threshold      0-100   darker than this becomes transparent (luma key)
    luma_softness       0-100
"""
import math
import os
from fractions import Fraction

import av
import numpy as np

THREADS = max(2, min(8, (os.cpu_count() or 4)))


def _f(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def geometry(cfg, src_w, src_h):
    """Where the CG lands: crop box (source px), scaled size, and top-left offset from cfg x/y."""
    crop = cfg.get("crop") or []
    if len(crop) == 4:
        x1, y1, x2, y2 = [int(round(_f(c, 0))) for c in crop]
        x1, y1 = max(0, min(x1, src_w - 2)), max(0, min(y1, src_h - 2))
        x2, y2 = max(x1 + 2, min(x2 or src_w, src_w)), max(y1 + 2, min(y2 or src_h, src_h))
    else:
        x1, y1, x2, y2 = 0, 0, src_w, src_h
    zoom = max(0.01, _f(cfg.get("zoom"), 100) / 100)
    sx = zoom * max(0.01, _f(cfg.get("zoom_x"), 1))
    sy = zoom * max(0.01, _f(cfg.get("zoom_y"), 1))
    w = max(2, int(round((x2 - x1) * sx)) // 2 * 2)
    h = max(2, int(round((y2 - y1) * sy)) // 2 * 2)
    # cropping hides edges but leaves the rest of the picture where it was
    return (x1, y1, x2, y2), (w, h), (int(round(x1 * sx)), int(round(y1 * sy)))


def needs_fx(cfg):
    return bool((cfg.get("key") or "none") != "none" or _f(cfg.get("rotate"), 0) % 360
                or len(cfg.get("crop") or []) == 4)


def _hex(c):
    c = (c or "#00ff00").lstrip("#")
    return "0x" + (c if len(c) == 6 else "00ff00")


class FxGraph:
    """FFmpeg filter graph for one CG; push decoded frames, get BGRA numpy images back."""

    def __init__(self, cfg, src_w, src_h, src_format):
        self.cfg = cfg
        (x1, y1, x2, y2), (w, h), self.offset = geometry(cfg, src_w, src_h)
        self.size = (w, h)
        self.rotate = _f(cfg.get("rotate"), 0) % 360
        self.trim = needs_fx(cfg)
        g = av.filter.Graph()
        g.threads = THREADS
        nodes = [g.add_buffer(width=src_w, height=src_h, format=src_format, time_base=Fraction(1, 1000))]
        add = lambda name, args="": nodes.append(g.add(name, args))
        if (x1, y1, x2, y2) != (0, 0, src_w, src_h):
            add("crop", f"w={x2 - x1}:h={y2 - y1}:x={x1}:y={y1}")
        add("scale", f"w={w}:h={h}:flags=bicubic")
        key = cfg.get("key") or "none"
        if key == "chroma":
            sim = 0.01 + 0.5 * _f(cfg.get("key_similarity"), 25) / 100
            blend = 0.3 * _f(cfg.get("key_blend"), 15) / 100
            add("format", "yuva420p")
            add("chromakey", f"color={_hex(cfg.get('key_color'))}:similarity={sim:.4f}:blend={blend:.4f}")
            spill = _f(cfg.get("key_spill"), 0) / 100
            if spill > 0:
                kind = "blue" if _is_blue(cfg.get("key_color")) else "green"
                add("format", "rgba")
                add("despill", f"type={kind}:mix={spill:.3f}:expand=0:alpha=0")
        elif key == "luma":
            thr = _f(cfg.get("luma_threshold"), 10) / 100
            soft = _f(cfg.get("luma_softness"), 5) / 100
            add("format", "yuva444p")
            add("lumakey", f"threshold={thr:.4f}:tolerance={max(0.001, soft):.4f}:softness={soft:.4f}")
        if self.rotate:
            a = self.rotate * math.pi / 180
            add("format", "rgba")
            add("rotate", f"a={a:.6f}:ow=rotw({a:.6f}):oh=roth({a:.6f}):c=none:bilinear=1")
            rw = abs(w * math.cos(a)) + abs(h * math.sin(a))
            rh = abs(w * math.sin(a)) + abs(h * math.cos(a))
            self.offset = (self.offset[0] - int((rw - w) / 2), self.offset[1] - int((rh - h) / 2))
        add("format", "bgra")
        nodes.append(g.add("buffersink"))
        for a_, b_ in zip(nodes, nodes[1:]):
            a_.link_to(b_)
        g.configure()
        self.graph = g
        self._pts = 0

    def process(self, frame):
        """VideoFrame in -> (bgra ndarray h x w x 4, (dx, dy) offset from the CG's x / y)."""
        frame.pts = self._pts
        self._pts += 40
        self.graph.push(frame)
        out = self.graph.pull()
        img = out.to_ndarray()
        if img.ndim == 2:
            img = img.reshape(out.height, out.width, 4)
        dx, dy = self.offset
        if self.trim:
            # cut away the fully transparent border so compositing only touches the visible part
            a = img[..., 3]
            rows = np.flatnonzero(a.any(axis=1))
            if rows.size == 0:
                return None, (dx, dy)
            cols = np.flatnonzero(a[rows[0]:rows[-1] + 1].any(axis=0))
            img = img[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
            dx, dy = dx + int(cols[0]), dy + int(rows[0])
        return np.ascontiguousarray(img), (dx, dy)


def _is_blue(c):
    c = (c or "#00ff00").lstrip("#")
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return False
    return b > g


def image_frame(path):
    """A still image file as an RGBA VideoFrame (keeps PNG alpha)."""
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    return av.VideoFrame.from_ndarray(np.asarray(im), format="rgba")


def first_frame(path, t=0.0):
    """Decoded VideoFrame of a video file at about t seconds."""
    with av.open(path) as c:
        vs = c.streams.video[0]
        if t > 0 and vs.time_base:
            c.seek(int(t / vs.time_base), stream=vs, any_frame=False, backward=True)
        last = None
        for frame in c.decode(vs):
            last = frame
            if frame.time is None or frame.time >= t - 0.02:
                break
        if last is None:
            raise ValueError("no picture in file")
        return last.reformat(format="rgba") if "a" in (last.format.name or "") else last


def render_still(path, kind, cfg, t=0.0):
    """Processed CG picture: (bgra ndarray, (dx, dy)) for previews and still CGs."""
    frame = image_frame(path) if kind == "image" else first_frame(path, t)
    fx = FxGraph(cfg, frame.width, frame.height, frame.format.name)
    return fx.process(frame)


def checker(w, h, cell=16):
    yy, xx = np.mgrid[0:h, 0:w]
    c = (((yy // cell) + (xx // cell)) % 2).astype(np.uint8)
    img = np.empty((h, w, 4), np.uint8)
    img[..., :3] = (np.where(c, 60, 38))[..., None]
    img[..., 3] = 255
    return img
