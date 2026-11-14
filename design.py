"""Layered CG designs made in CG Composer's Designer: text, shapes and pictures on one canvas.

A design is a text CG ("kind": "text") with "style": "design", so Playout plays it like any other
text CG (IN / OUT, fade, wipe, slide, live cards) and the operator changes its fields while on air:

    design      {"w": 1920, "h": 1080, "layers": [layer, ...]}   the canvas (positions in its pixels)
    fields      {"name": "Kamal Perera", ...}   text typed in Playout for the layers that have a field

A layer (first in the list is at the back):
    type        "text" | "rect" | "ellipse" | "image"
    name        shown in the layer list (and as the field's label)
    x, y, w, h  box on the canvas;  rotate  degrees clockwise;  opacity  0-100;  hidden
    fill        "#rrggbb"; fill2 + grad ("v" / "h") a gradient to fill2; "" = no fill
    radius      round corners (rect);  stroke, stroke_w  outline colour and width
    text        text (new lines allowed);  field  name of the field that replaces it ("" = fixed text)
    font, size, bold, color, align ("left" / "center" / "right"), upper (capitals), shadow
    fit         make the text smaller when it does not fit in the box (default on)
    src         picture file in the CG folder (image layer);  mode  "contain" | "cover" | "stretch"

render() draws it at the output size and gives back the picture cropped to what is drawn, with its place.
"""
import copy
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

IMAGE_DIRS = []                 # where picture layers are found (the CG folder)
_img_cache = {}


def set_image_dirs(*dirs):
    IMAGE_DIRS[:] = [d for d in dirs if d]


def _t():
    import textcg
    return textcg


def _rgb(c, default=None):
    c = str(c or "").lstrip("#")
    if len(c) != 6:
        return default
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fields_of(design):
    """[(field, label, default text)] of a design's text layers, in layer order (no repeats)."""
    out, seen = [], set()
    for L in (design or {}).get("layers") or []:
        f = str(L.get("field") or "").strip()
        if L.get("type") == "text" and f and f not in seen:
            seen.add(f)
            out.append((f, L.get("name") or f, L.get("text") or ""))
    return out


def _find_image(name):
    name = os.path.basename(str(name or ""))
    for d in IMAGE_DIRS:
        p = os.path.join(d, name)
        if name and os.path.isfile(p):
            return p
    return None


def _picture(src, w, h, mode):
    p = _find_image(src)
    if not p or w < 1 or h < 1:
        return None
    key = (p, os.path.getmtime(p), w, h, mode)
    im = _img_cache.get(key)
    if im is None:
        try:
            with Image.open(p) as f:
                f.seek(0)
                pic = f.convert("RGBA")
        except Exception:
            return None
        if mode == "stretch":
            im = pic.resize((w, h), Image.LANCZOS)
        else:
            k = (max if mode == "cover" else min)(w / pic.width, h / pic.height)
            nw, nh = max(1, round(pic.width * k)), max(1, round(pic.height * k))
            pic = pic.resize((nw, nh), Image.LANCZOS)
            im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            im.alpha_composite(pic.crop((max(0, (nw - w) // 2), max(0, (nh - h) // 2),
                                         max(0, (nw - w) // 2) + min(w, nw), max(0, (nh - h) // 2) + min(h, nh))),
                               (max(0, (w - nw) // 2), max(0, (h - nh) // 2)))
        if len(_img_cache) > 64:
            _img_cache.clear()
        _img_cache[key] = im
    return im


def _fill_image(L, w, h):
    """The layer's fill as a picture (plain colour or gradient), or None."""
    a = _rgb(L.get("fill"))
    if a is None:
        return None
    b = _rgb(L.get("fill2"))
    g = L.get("grad") or ""
    if b is None or g not in ("v", "h"):
        return Image.new("RGBA", (w, h), a + (255,))
    t = np.linspace(0, 1, max(1, h if g == "v" else w), dtype=np.float32)
    t = t[:, None, None] if g == "v" else t[None, :, None]
    ca, cb = np.array(a, np.float32), np.array(b, np.float32)
    rgb = (ca + (cb - ca) * t) * np.ones((h, w, 1), np.float32)
    al = np.full((h, w, 1), 255, np.float32)
    return Image.fromarray(np.concatenate([rgb, al], 2).astype(np.uint8), "RGBA")


def _shape(L, w, h, k):
    """rect / ellipse drawn at 2x and scaled down (smooth edges)."""
    smooth = L.get("type") == "ellipse" or _num(L.get("radius")) > 0
    S = 2 if smooth and w * h <= 1_000_000 else 1
    W, H = w * S, h * S
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    sw = max(0, int(round(_num(L.get("stroke_w")) * k * S)))
    r = max(0, int(round(_num(L.get("radius")) * k * S)))
    box = [0, 0, W - 1, H - 1]
    ell = L.get("type") == "ellipse"
    if ell:
        d.ellipse(box, fill=255)
    else:
        d.rounded_rectangle(box, radius=min(r, W // 2, H // 2), fill=255)
    fill = _fill_image(L, W, H)
    if fill is not None:
        im.paste(fill, (0, 0), mask)
    sc = _rgb(L.get("stroke"))
    if sc is not None and sw > 0:
        ring = Image.new("L", (W, H), 0)
        rd = ImageDraw.Draw(ring)
        if ell:
            rd.ellipse(box, outline=255, width=sw)
        else:
            rd.rounded_rectangle(box, radius=min(r, W // 2, H // 2), outline=255, width=sw)
        im.paste(Image.new("RGBA", (W, H), sc + (255,)), (0, 0), ring)
    return im.resize((w, h), Image.LANCZOS) if S > 1 else im


def _text(L, text, w, h, k):
    t = _t()
    if L.get("upper"):
        text = text.upper()
    size = max(6, int(round(_num(L.get("size"), 48) * k)))
    bold = bool(L.get("bold", True))
    fontname = t._fontname({"font": L.get("font") or "Arial", "text": text})
    lines = text.split("\n")

    def measure(sz):
        f = t.get_font(fontname, sz, bold)
        asc, desc = f.getmetrics()
        lh = int((asc + desc) * 1.12)
        widths = [t._text_w(f, s) for s in lines]
        return f, lh, widths

    f, lh, widths = measure(size)
    if L.get("fit", True):
        while size > 8 and (max(widths or [0]) > w or lh * len(lines) > h * 1.15):
            size = max(8, int(size * 0.92))
            f, lh, widths = measure(size)
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    color = _rgb(L.get("color"), (255, 255, 255)) + (255,)
    align = L.get("align") or "left"
    sc = _rgb(L.get("stroke"))
    sw = max(0, int(round(_num(L.get("stroke_w")) * k)))
    top = (h - lh * len(lines)) / 2
    shadow = bool(L.get("shadow"))
    sh = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if shadow else None
    sd = ImageDraw.Draw(sh) if shadow else None
    off = max(2, size // 18)
    for i, s in enumerate(lines):
        x = 0 if align == "left" else (w - widths[i]) / 2 if align == "center" else w - widths[i]
        y = top + lh * i + lh / 2
        kw = {"stroke_width": sw, "stroke_fill": sc + (255,)} if sc is not None and sw > 0 else {}
        if shadow:
            sd.text((x + off, y + off), s, font=f, fill=(0, 0, 0, 170), anchor="lm", **kw)
        d.text((x, y), s, font=f, fill=color, anchor="lm", **kw)
    if shadow:
        sh = sh.filter(ImageFilter.GaussianBlur(max(1, size // 20)))
        sh.alpha_composite(im)
        im = sh
    return im


def layer_pictures(cfg, out_w, out_h):
    """Every visible layer drawn on its own at the output size: [(layer, RGBA picture, x, y)], back to front.
    x, y is where the (rotated) picture's top-left goes on the output."""
    design = cfg.get("design") or {}
    dw, dh = _num(design.get("w"), out_w) or out_w, _num(design.get("h"), out_h) or out_h
    k = min(out_w / dw, out_h / dh)
    fields = cfg.get("fields") or {}
    out = []
    for L in design.get("layers") or []:
        if L.get("hidden"):
            continue
        w, h = int(round(_num(L.get("w"), 100) * k)), int(round(_num(L.get("h"), 100) * k))
        if w < 1 or h < 1:
            continue
        kind = L.get("type")
        try:
            if kind == "text":
                f = str(L.get("field") or "")
                text = str(fields.get(f, L.get("text") or "") if f else (L.get("text") or ""))
                if not text.strip():
                    continue
                im = _text(L, text, w, h, k)
            elif kind in ("rect", "ellipse"):
                im = _shape(L, w, h, k)
            elif kind == "image":
                im = _picture(L.get("src"), w, h, L.get("mode") or "contain")
                if im is None:
                    continue
                im = im.copy()
            else:
                continue
        except Exception:
            continue
        op = max(0.0, min(100.0, _num(L.get("opacity"), 100))) / 100
        if op < 1:
            a = np.asarray(im.getchannel("A"), np.float32) * op
            im.putalpha(Image.fromarray(a.astype(np.uint8), "L"))
        cx, cy = (_num(L.get("x")) * k) + w / 2, (_num(L.get("y")) * k) + h / 2
        rot = _num(L.get("rotate")) % 360
        if rot:
            im = im.rotate(-rot, resample=Image.BICUBIC, expand=True)
        out.append((L, im, int(round(cx - im.width / 2)), int(round(cy - im.height / 2))))
    return out


def render_full(cfg, out_w, out_h):
    """The whole design at the output size: RGBA PIL image (see-through where nothing is drawn)."""
    canvas = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
    for _L, im, x, y in layer_pictures(cfg, out_w, out_h):
        part, at = _clip(im, x, y, out_w, out_h)
        canvas.alpha_composite(part, at)
    return canvas


# ---------------------------------------------------------------- animation (in / out, layer by layer)
# A layer's "anim_in" / "anim_out": {"type": ..., "delay": seconds, "dur": seconds}. Types:
#   none | fade | slide-l | slide-r | slide-u | slide-d (comes from that side, fading) | wipe-l | wipe-r
#   (revealed from the left / right) | scale (grows from smaller, fading) | pop (grows past its size and settles)
# Without its own, a layer takes the design's "anim" (fade / wipe / slide / up) over "fade" frames, as Playout did.
ANIMS = ("none", "fade", "slide-l", "slide-r", "slide-u", "slide-d", "wipe-l", "wipe-r", "scale", "pop", "dwipe-l", "dwipe-r")
# dwipe-l / dwipe-r: one wipe across the whole design (every layer cut at the same edge), for a Take transition
_OLD = {"fade": "fade", "wipe": "wipe-l", "slide": "slide-l", "up": "slide-u"}


def _ease(p):
    return 1 - (1 - p) ** 3


def _anim_of(cfg, L, phase, fps):
    own = L.get("anim_in" if phase == "in" else "anim_out")
    if phase == "out" and not (own or {}).get("type"):
        own = L.get("anim_in")                       # going out the way it came in, backwards
        own = {**own, "delay": 0} if own else own
    if own and own.get("type"):
        return own.get("type"), max(0.0, _num(own.get("delay"))), max(0.0, _num(own.get("dur"), 0.5))
    frames = max(0, int(_num(cfg.get("fade"), 12)))
    return _OLD.get(cfg.get("anim") or "fade", "fade"), 0.0, frames / float(fps or 25)


class Animator:
    """A design's layers made once, then put together for any moment of its coming in / going out.

    state(phase, t): [(i, x, y, alpha 0-1, scale, cut_left 0-1, cut_right 0-1)] for each layer showing
    compose(states): the output picture (RGBA PIL) for those states"""

    def __init__(self, cfg, out_w, out_h, fps=25):
        self.cfg, self.w, self.h, self.fps = cfg, out_w, out_h, float(fps or 25)
        self.layers = layer_pictures(cfg, out_w, out_h)
        self.anims = {ph: [_anim_of(cfg, L, ph, self.fps) for L, *_ in self.layers] for ph in ("in", "out")}
        self.bx = (min([x for _L, _im, x, _y in self.layers] or [0]),
                   max([x + im.width for _L, im, x, _y in self.layers] or [1]))

    def length(self, phase):
        return max([d + du for _t, d, du in self.anims[phase] if _t != "none"] or [0.0])

    def state(self, phase, t):
        out = []
        for i, (L, im, x, y) in enumerate(self.layers):
            kind, delay, dur = self.anims["in" if phase == "on" else phase][i]
            if phase == "on" or kind == "none":       # a layer without animation: there until the others are out
                if phase != "out" or t < self.length("out"):
                    out.append((i, x, y, 1.0, 1.0, 0.0, 0.0))
                continue
            q = 1.0 if dur <= 0 else min(1.0, max(0.0, (t - delay) / dur))
            p = q if phase == "in" else 1 - q           # 0 = gone, 1 = in place
            if p <= 0:
                continue
            e = _ease(p) if phase == "in" else 1 - _ease(1 - p)
            dx = dy = 0
            a, sc, cl, cr = 1.0, 1.0, 0.0, 0.0
            if kind == "fade":
                a = e
            elif kind.startswith("slide"):
                dist_x = max(60, min(500, im.width * 0.6))
                dist_y = max(40, min(300, im.height * 0.8))
                d = {"slide-l": (-dist_x, 0), "slide-r": (dist_x, 0), "slide-u": (0, dist_y), "slide-d": (0, -dist_y)}[kind]
                dx, dy = int(d[0] * (1 - e)), int(d[1] * (1 - e))
                a = min(1.0, e * 1.6)
            elif kind == "wipe-l":
                cr = 1 - e
            elif kind == "wipe-r":
                cl = 1 - e
            elif kind in ("dwipe-l", "dwipe-r"):        # one edge moving across the whole design
                b0, b1 = self.bx
                if kind == "dwipe-l":
                    edge = b0 + (b1 - b0) * e
                    cr = min(1.0, max(0.0, (x + im.width - edge) / im.width))
                else:
                    edge = b1 - (b1 - b0) * e
                    cl = min(1.0, max(0.0, (edge - x) / im.width))
                if cl >= 1 or cr >= 1:
                    continue
            elif kind == "scale":
                sc, a = 0.6 + 0.4 * e, e
            elif kind == "pop":
                sc = (0.5 + 0.62 * e) if e < 0.85 else (1.027 - 0.18 * (e - 0.85))
                a = min(1.0, e * 2)
            out.append((i, x + dx, y + dy, a, sc, cl, cr))
        return out

    def compose(self, states):
        canvas = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        for i, x, y, a, sc, cl, cr in states:
            im = self.layers[i][1]
            if cl > 0 or cr > 0:
                x0, x1 = int(im.width * cl), int(im.width * (1 - cr))
                if x1 <= x0:
                    continue
                im, x = im.crop((x0, 0, x1, im.height)), x + x0
            if abs(sc - 1) > 0.001:
                nw, nh = max(1, int(im.width * sc)), max(1, int(im.height * sc))
                x, y = x + (im.width - nw) // 2, y + (im.height - nh) // 2
                im = im.resize((nw, nh), Image.BILINEAR)
            if a < 0.999:
                im = im.copy()
                im.putalpha(im.getchannel("A").point(lambda v, a=a: int(v * a)))
            part, at = _clip(im, x, y, self.w, self.h)
            canvas.alpha_composite(part, at)
        return canvas


def _clip(im, x, y, W, H):
    """im placed at x, y, cut to the W x H canvas -> (part, (x, y)) (alpha_composite needs it inside)."""
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + im.width), min(H, y + im.height)
    if x1 <= x0 or y1 <= y0:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), (0, 0)
    return im.crop((x0 - x, y0 - y, x1 - x, y1 - y)), (x0, y0)


def render(cfg, out_w, out_h):
    """-> (BGRA numpy array cropped to what is drawn, x, y), or (None, 0, 0) when nothing shows."""
    canvas = render_full(cfg, out_w, out_h)
    box = canvas.getchannel("A").getbbox()
    if not box:
        return None, 0, 0
    part = canvas.crop(box)
    a = np.asarray(part)
    return np.ascontiguousarray(a[..., [2, 1, 0, 3]]), box[0], box[1]


# ---------------------------------------------------------------- starter templates (1920 x 1080 canvas)
NAVY, ORANGE, RED, GOLD, WHITE, DARK = "#0b1a3a", "#ff8a1c", "#e0261b", "#c8a96a", "#ffffff", "#0c0d0f"


def _L(type_, name, x, y, w, h, **kw):
    d = {"type": type_, "name": name, "x": x, "y": y, "w": w, "h": h, "rotate": 0, "opacity": 100}
    if type_ == "text":
        d.update(font="Arial", size=48, bold=True, color=WHITE, align="left", fit=True)
    d.update(kw)
    return d


def _tx(name, x, y, w, h, text, field="", **kw):
    return _L("text", name, x, y, w, h, text=text, field=field, **kw)


TEMPLATES = {
    "lower3": ("Lower third", [
        _L("rect", "Bar", 120, 820, 900, 96, fill=NAVY, fill2="#16306a", grad="h", radius=6),
        _L("rect", "Accent", 120, 820, 12, 150, fill=ORANGE),
        _L("rect", "Title bar", 132, 916, 640, 54, fill=ORANGE),
        _tx("Name", 160, 826, 840, 84, "Name Surname", "name", size=56),
        _tx("Title", 152, 918, 600, 50, "Title / Position", "title", size=32, color=DARK),
    ]),
    "presenter": ("Presenter name", [
        _L("rect", "Card", 100, 840, 760, 120, fill="#101317", radius=60, opacity=88),
        _L("ellipse", "Dot", 128, 868, 64, 64, fill=ORANGE),
        _tx("Presenter", 214, 846, 620, 64, "Presenter Name", "name", size=50),
        _tx("Role", 214, 906, 620, 40, "Host", "role", size=28, bold=False, color="#c9ccd1"),
    ]),
    "breaking": ("Breaking news", [
        _L("rect", "Band", 0, 860, 1920, 130, fill="#14161a", opacity=95),
        _L("rect", "Label", 0, 860, 430, 130, fill=RED, fill2="#9e140c", grad="v"),
        _tx("Label text", 30, 870, 380, 110, "BREAKING NEWS", "label", size=52, align="center"),
        _tx("Headline", 470, 870, 1410, 110, "Headline goes here", "headline", size=54),
    ]),
    "wedding": ("Wedding title", [
        _L("rect", "Glow", 460, 760, 1000, 230, fill="#000000", opacity=45, radius=24),
        _L("rect", "Line top", 610, 780, 700, 3, fill=GOLD),
        _tx("Couple", 480, 790, 960, 120, "Nadeesha & Kasun", "couple", font="Segoe UI", size=84, bold=False,
            color="#f5e6c4", align="center", shadow=True),
        _tx("Date", 480, 912, 960, 52, "Wedding Ceremony  |  14 November 2026", "date", size=32, bold=False,
            color=GOLD, align="center"),
        _L("rect", "Line bottom", 610, 970, 700, 3, fill=GOLD),
    ]),
    "event": ("Event title", [
        _L("rect", "Box", 260, 380, 1400, 320, fill=NAVY, fill2="#040a18", grad="v", radius=10, opacity=92),
        _L("rect", "Accent", 260, 380, 1400, 10, fill=ORANGE),
        _tx("Event", 300, 420, 1320, 160, "EVENT NAME", "event", size=110, align="center"),
        _tx("Subtitle", 300, 590, 1320, 70, "Venue  |  Date", "subtitle", size=42, bold=False, color="#d6dae0",
            align="center"),
    ]),
    "sponsor": ("Sponsor logo", [
        _L("rect", "Card", 1480, 60, 380, 200, fill=WHITE, radius=14, opacity=94),
        _tx("Label", 1500, 70, 340, 40, "SPONSORED BY", "label", size=24, color="#555a62", align="center"),
        _L("image", "Logo", 1510, 112, 320, 132, src="", mode="contain"),
    ]),
    "cricket": ("Cricket scoreboard", [
        _L("rect", "Bar", 60, 960, 1300, 80, fill="#101317", opacity=94, radius=6),
        _L("rect", "Team box", 60, 960, 240, 80, fill=NAVY, radius=6),
        _tx("Team", 70, 964, 220, 72, "SL", "team", size=46, align="center"),
        _tx("Score", 320, 964, 300, 72, "152/4", "score", size=52),
        _tx("Overs", 620, 970, 230, 60, "18.2 ov", "overs", size=34, bold=False, color="#c9ccd1"),
        _L("rect", "Target box", 870, 970, 470, 60, fill=ORANGE, radius=4),
        _tx("Target", 880, 972, 450, 56, "Need 24 from 10", "target", size=32, color=DARK, align="center"),
    ]),
    "football": ("Football scoreboard", [
        _L("rect", "Home", 60, 50, 230, 64, fill=NAVY),
        _tx("Home team", 66, 52, 218, 60, "HOME", "home", size=38, align="center"),
        _L("rect", "Score box", 290, 50, 160, 64, fill=WHITE),
        _tx("Score", 296, 52, 148, 60, "0 - 0", "score", size=40, color=DARK, align="center"),
        _L("rect", "Away", 450, 50, 230, 64, fill=RED),
        _tx("Away team", 456, 52, 218, 60, "AWAY", "away", size=38, align="center"),
        _L("rect", "Time box", 680, 50, 140, 64, fill="#101317"),
        _tx("Time", 686, 52, 128, 60, "45:00", "time", size=36, align="center", color=ORANGE),
    ]),
    "lineup": ("Team lineup", [
        _L("rect", "Panel", 560, 90, 800, 900, fill="#0d1220", fill2="#050810", grad="v", radius=12, opacity=94),
        _L("rect", "Header", 560, 90, 800, 120, fill=NAVY, radius=12),
        _tx("Team", 590, 100, 740, 100, "TEAM NAME", "team", size=64, align="center"),
        _tx("Players", 620, 240, 680, 720, "1  Player One\n2  Player Two\n3  Player Three\n4  Player Four\n"
            "5  Player Five\n6  Player Six\n7  Player Seven\n8  Player Eight\n9  Player Nine\n10  Player Ten\n"
            "11  Player Eleven", "players", size=46, bold=False),
    ]),
    "player": ("Player profile", [
        _L("rect", "Panel", 120, 640, 1100, 340, fill="#0d1220", radius=14, opacity=94),
        _L("image", "Photo", 140, 660, 300, 300, src="", mode="cover"),
        _L("rect", "Accent", 470, 680, 8, 260, fill=ORANGE),
        _tx("Player", 500, 670, 700, 90, "PLAYER NAME", "name", size=64),
        _tx("Details", 500, 770, 700, 180, "Matches  45\nRuns  1,820\nAverage  42.3", "details", size=40,
            bold=False, color="#d6dae0"),
    ]),
    "result": ("Match result", [
        _L("rect", "Panel", 360, 300, 1200, 480, fill=NAVY, fill2="#050b1c", grad="v", radius=16, opacity=95),
        _tx("Heading", 400, 330, 1120, 80, "FULL TIME", "heading", size=56, color=ORANGE, align="center"),
        _tx("Home", 400, 450, 460, 110, "HOME", "home", size=64, align="center"),
        _tx("Score", 860, 440, 200, 130, "2 - 1", "score", size=84, align="center"),
        _tx("Away", 1060, 450, 460, 110, "AWAY", "away", size=64, align="center"),
        _tx("Note", 400, 640, 1120, 90, "Man of the match: Player Name", "note", size=38, bold=False,
            color="#d6dae0", align="center"),
    ]),
    "fullscreen": ("Full-screen graphic", [
        _L("rect", "Background", 0, 0, 1920, 1080, fill=NAVY, fill2="#03060f", grad="v"),
        _L("rect", "Accent", 0, 1040, 1920, 40, fill=ORANGE),
        _tx("Title", 160, 340, 1600, 200, "TITLE", "title", size=140, align="center"),
        _tx("Subtitle", 160, 560, 1600, 100, "Subtitle text", "subtitle", size=56, bold=False, color="#d6dae0",
            align="center"),
    ]),
    "blank": ("Blank", []),
}


def template(key):
    """A new design from a starter template: (name, design dict)."""
    name, layers = TEMPLATES.get(key) or TEMPLATES["blank"]
    layers = copy.deepcopy(layers)
    for i, L in enumerate(layers):
        L["id"] = f"l{i + 1}"
    return name, {"w": 1920, "h": 1080, "layers": layers}


def template_list():
    return [{"key": k, "name": v[0]} for k, v in TEMPLATES.items()]
