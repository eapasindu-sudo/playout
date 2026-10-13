"""Text CGs typed on screen: lower third (name / title), scrolling ticker, clock / date, plain text.

A text CG is a manual CG config with "kind": "text":
    style        "lower3" | "ticker" | "clock" | "text"
    line1, line2 lower third: name and title.  text: the text (new lines allowed)
    text         ticker: the news items, one per line (shown with a separator between)
    text_file    ticker: read the items from this .txt file instead (re-read every 10 s)
    label        ticker: box at the left ("NEWS", "BREAKING")
    fmt          clock: "%H:%M:%S", "%I:%M %p", "%d %B %Y", ... (Python strftime)
    font, size, bold
    color        text colour "#ffffff"
    bg, bg_alpha box colour and its opacity 0-100 (0 = no box)
    accent       lower third / ticker label colour
    x, y, width  position; width 0 = automatic (ticker: whole screen)
    speed        ticker: pixels per second
Sinhala and Tamil work with a font that has them (Iskoola Pota, Nirmala UI).
"""
import datetime as dt
import glob
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont, features

RAQM = features.check("raqm")      # proper Sinhala / Tamil shaping
TEXT_KEYS = ("style", "line1", "line2", "text", "text_file", "label", "fmt", "font", "size", "bold", "color",
             "bg", "bg_alpha", "accent", "x", "y", "width", "speed", "separator")

# font name -> (regular, bold) file names
WIN_FONTS = {
    "Arial": ("arial.ttf", "arialbd.ttf"),
    "Segoe UI": ("segoeui.ttf", "segoeuib.ttf"),
    "Calibri": ("calibri.ttf", "calibrib.ttf"),
    "Tahoma": ("tahoma.ttf", "tahomabd.ttf"),
    "Verdana": ("verdana.ttf", "verdanab.ttf"),
    "Impact": ("impact.ttf", "impact.ttf"),
    "Iskoola Pota (Sinhala)": ("iskpota.ttf", "iskpotab.ttf"),
    "Nirmala UI (Sinhala / Tamil)": ("Nirmala.ttf", "NirmalaB.ttf"),
    "Latha (Tamil)": ("latha.ttf", "lathab.ttf"),
}
_font_cache = {}
_extra_dirs = []


def set_font_dirs(*dirs):
    """Extra folders searched for .ttf / .otf files (the CG folder: drop a font in to use it)."""
    _extra_dirs[:] = [d for d in dirs if d]


def _font_dirs():
    out = list(_extra_dirs)
    if sys.platform == "win32":
        out.append(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
        out.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"))
    else:
        out += ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts")]
    return [d for d in out if d and os.path.isdir(d)]


def _find_file(name):
    low = name.lower()
    for d in _font_dirs():
        for p in glob.glob(os.path.join(d, "**", "*"), recursive=True):
            if os.path.basename(p).lower() == low:
                return p
    return None


def list_fonts():
    names = []
    for n, (reg, _) in WIN_FONTS.items():
        if _find_file(reg):
            names.append(n)
    for d in _extra_dirs:
        for p in sorted(glob.glob(os.path.join(d, "*.[ot]tf"))):
            names.append(os.path.basename(p))
    if not names:
        names.append("Default")
    return names


def get_font(name, size, bold=False):
    key = (name, int(size), bool(bold))
    if key in _font_cache:
        return _font_cache[key]
    path = None
    if name in WIN_FONTS:
        reg, bd = WIN_FONTS[name]
        path = _find_file(bd if bold else reg) or _find_file(reg)
    elif name and name.lower().endswith((".ttf", ".otf")):
        path = _find_file(name)
    if path is None:      # Linux test PC / missing font
        for cand in ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", "arial.ttf", "segoeui.ttf"):
            path = _find_file(cand)
            if path:
                break
    kw = {"layout_engine": ImageFont.Layout.RAQM} if RAQM else {}
    try:
        f = ImageFont.truetype(path, int(size), **kw) if path else ImageFont.load_default(int(size))
    except Exception:
        f = ImageFont.load_default(int(size))
    _font_cache[key] = f
    return f


def _rgb(c, default=(255, 255, 255)):
    c = (c or "").lstrip("#")
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _bgra(im):
    """PIL RGBA image -> contiguous BGRA numpy array."""
    a = np.asarray(im.convert("RGBA"))
    return np.ascontiguousarray(a[..., [2, 1, 0, 3]])


def _text_w(font, s):
    if not s:
        return 0
    b = font.getbbox(s)
    return b[2] - b[0] if b else 0


def render_static(cfg, out_w, out_h, now=None):
    """Lower third / clock / plain text -> BGRA array (cropped to its size)."""
    style = cfg.get("style") or "text"
    size = int(_num(cfg.get("size"), 44))
    font = get_font(cfg.get("font") or "Arial", size, cfg.get("bold", True))
    color = _rgb(cfg.get("color"), (255, 255, 255)) + (255,)
    bg = _rgb(cfg.get("bg"), (10, 30, 80))
    bg_a = int(255 * max(0, min(100, _num(cfg.get("bg_alpha"), 85))) / 100)
    accent = _rgb(cfg.get("accent"), (224, 38, 27)) + (255,)
    pad = max(6, size // 3)
    if style == "lower3":
        f2 = get_font(cfg.get("font") or "Arial", max(10, int(size * 0.62)), False)
        l1, l2 = cfg.get("line1") or "", cfg.get("line2") or ""
        w1, w2 = _text_w(font, l1), _text_w(f2, l2)
        h1, h2 = int(size * 1.35), int(size * 0.62 * 1.4) if l2 else 0
        width = int(_num(cfg.get("width"), 0)) or max(w1, w2) + pad * 3 + 10
        width = min(width, out_w)
        im = Image.new("RGBA", (width, h1 + h2 + pad), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, width, h1], fill=bg + (bg_a,))
        d.rectangle([0, 0, 9, h1 + h2 + pad], fill=accent)
        d.text((pad + 12, h1 // 2), l1, font=font, fill=color, anchor="lm")
        if l2:
            d.rectangle([10, h1, width, h1 + h2 + pad], fill=(accent[0], accent[1], accent[2], bg_a))
            d.text((pad + 12, h1 + (h2 + pad) // 2), l2, font=f2, fill=color, anchor="lm")
        return _bgra(im)
    if style == "clock":
        now = now or dt.datetime.now()
        lines = []
        for fmt in (cfg.get("fmt") or "%H:%M:%S").replace("\\n", "\n").split("\n"):
            try:
                lines.append(now.strftime(fmt))
            except ValueError:
                lines.append(fmt)
        text = "\n".join(lines)
    else:
        text = cfg.get("text") or ""
    if not text.strip():
        return None
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = tmp.multiline_textbbox((0, 0), text, font=font, spacing=size // 4)
    tw, th = box[2] - box[0], box[3] - box[1]
    width = int(_num(cfg.get("width"), 0)) or tw + pad * 2
    im = Image.new("RGBA", (min(width, out_w), th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if bg_a:
        d.rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=pad // 2, fill=bg + (bg_a,))
    align = "center" if cfg.get("width") else "left"
    x = (im.width - tw) // 2 - box[0] if align == "center" else pad - box[0]
    d.multiline_text((x, pad - box[1]), text, font=font, fill=color, spacing=size // 4, align=align)
    return _bgra(im)


class TextLayer:
    """A text CG on air. Same interface as cg.CGLayer (tick / take_in / take_out / state)."""

    def __init__(self, cfg, out_w, out_h, fps):
        self.cfg = dict(cfg)
        self.out_w, self.out_h = out_w, out_h
        self.fps = float(fps)
        self.fade = max(0, int(cfg.get("fade", 8)))
        self.delay_frames = int(_num(cfg.get("delay"), 0) * self.fps)
        dur = _num(cfg.get("duration"), 0)
        self.duration_frames = int(dur * self.fps) if dur > 0 else None
        self.state = "wait" if self.delay_frames else "in"
        self.level = 0
        self.ticks = self.on_ticks = 0
        self.error = ""
        self.audio = None
        self._img = None
        self._clock_key = None
        self._strip = None
        self._pos = 0.0
        self._file_check = 0.0
        self._file_text = None
        self._build()

    # ---- content --------------------------------------------------------
    def update(self, cfg):
        """New text / look while on air (the ticker keeps scrolling from where it is)."""
        self.cfg.update(cfg)
        self._build()

    def _ticker_text(self):
        f = self.cfg.get("text_file")
        if f and os.path.isfile(f):
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    self._file_text = fh.read()
            except OSError:
                pass
            return self._file_text or ""
        return self.cfg.get("text") or ""

    def _build(self):
        style = self.cfg.get("style") or "text"
        try:
            if style == "ticker":
                self._build_ticker()
            elif style != "clock":
                self._img = render_static(self.cfg, self.out_w, self.out_h)
            self.error = ""
        except Exception as e:
            self.error = str(e)

    def _build_ticker(self):
        cfg = self.cfg
        size = int(_num(cfg.get("size"), 40))
        font = get_font(cfg.get("font") or "Arial", size, cfg.get("bold", True))
        sep = cfg.get("separator") or "   \u25cf   "
        items = [s.strip() for s in self._ticker_text().splitlines() if s.strip()]
        text = sep.join(items) + sep if items else " "
        h = int(size * 1.6)
        tw = max(1, _text_w(font, text))
        strip = Image.new("RGBA", (tw, h), (0, 0, 0, 0))
        ImageDraw.Draw(strip).text((0, h // 2), text, font=font, fill=_rgb(cfg.get("color")) + (255,), anchor="lm")
        self._strip = _bgra(strip)
        width = int(_num(cfg.get("width"), 0)) or self.out_w
        self._box_w = min(width, self.out_w)
        bg = _rgb(cfg.get("bg"), (10, 30, 80))
        bg_a = int(255 * max(0, min(100, _num(cfg.get("bg_alpha"), 90))) / 100)
        base = Image.new("RGBA", (self._box_w, h), bg + (bg_a,))
        self._label_w = 0
        label = cfg.get("label") or ""
        if label:
            lf = get_font(cfg.get("font") or "Arial", size, True)
            lw = _text_w(lf, label) + size
            d = ImageDraw.Draw(base)
            d.rectangle([0, 0, lw, h], fill=_rgb(cfg.get("accent"), (224, 38, 27)) + (255,))
            d.text((lw // 2, h // 2), label, font=lf, fill=(255, 255, 255, 255), anchor="mm")
            self._label_w = lw + 4
        self._base = _bgra(base)
        self._frame = self._base.copy()
        if self._pos == 0:
            self._pos = float(self._box_w - self._label_w)   # start off the right edge

    def _ticker_frame(self):
        if time.time() - self._file_check > 10 and self.cfg.get("text_file"):
            self._file_check = time.time()
            old = self._file_text
            self._ticker_text()
            if self._file_text != old:
                self._build_ticker()
        out = self._frame
        out[:] = self._base
        area_x, area_w = self._label_w, self._box_w - self._label_w
        sw = self._strip.shape[1]
        x = int(self._pos)
        # draw the strip repeatedly so the text runs round without a gap
        start = x if x > 0 else x % sw - sw
        while start < area_w:
            a0, a1 = max(0, start), min(area_w, start + sw)
            if a1 > a0:
                src = self._strip[:, a0 - start:a1 - start]
                dst = out[:, area_x + a0:area_x + a1]
                al = src[..., 3:4].astype(np.uint16)
                dst[..., :3] = ((src[..., :3] * al + dst[..., :3] * (255 - al) + 127) // 255).astype(np.uint8)
            start += sw
        self._pos -= _num(self.cfg.get("speed"), 120) / self.fps
        if self._pos < -sw:
            self._pos += sw
        return out

    def _picture(self):
        style = self.cfg.get("style") or "text"
        if style == "ticker":
            return self._ticker_frame() if self._strip is not None else None
        if style == "clock":
            now = dt.datetime.now()
            key = now.replace(microsecond=0)
            if key != self._clock_key:
                self._clock_key = key
                self._img = render_static(self.cfg, self.out_w, self.out_h, now)
        return self._img

    # ---- on air ---------------------------------------------------------
    def take_out(self):
        if self.state == "wait":
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
        self.ticks += 1
        if self.state == "off":
            return None
        if self.state == "wait":
            if self.ticks < self.delay_frames:
                return None
            self.state = "in"
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
        img = self._picture()
        if img is None:
            return None
        opacity = 255 if (not self.fade or self.state == "on") else int(255 * self.level / self.fade)
        return img, opacity, (0, 0)

    def close(self):
        self._strip = self._img = None


def preview(cfg, out_w, out_h):
    """Picture of a text CG for the editor (ticker: a still of its start)."""
    layer = TextLayer({**cfg, "fade": 0}, out_w, out_h, 25)
    if (cfg.get("style") or "") == "ticker":
        layer._pos = 0.0
    r = layer.tick()
    if r is None:
        return None, layer.error
    return r[0].copy(), layer.error
