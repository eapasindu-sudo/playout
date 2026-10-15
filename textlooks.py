"""Looks (templates) for text CGs: news, glass, sports, minimal, elegant, pill, and the newer
streaming styles neon (glow), split (colour block + white card), gradient and frame (corner brackets).

"look" in a text CG config picks one; "classic" (or empty) is the original flat box drawn in
textcg.py. Everything is drawn at twice the size and scaled down, so slanted edges and round
corners are smooth on air. Text on a coloured bar switches to black or white by itself when
the chosen text colour would not be readable on it.
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

LOOKS = ("classic", "news", "glass", "sports", "minimal", "elegant", "pill", "neon", "split", "gradient", "frame")
SS = 2          # drawn at 2x, then scaled down (smooth edges)


def _t():
    import textcg
    return textcg


def _lum(rgb):
    r, g, b = [c / 255 for c in rgb[:3]]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ink(bar, color):
    """The text colour, or black / white when it would not read on this bar."""
    if abs(_lum(bar) - _lum(color)) >= 0.4:
        return tuple(color[:3]) + (255,)
    return (20, 20, 24, 255) if _lum(bar) > 0.55 else (255, 255, 255, 255)


def _shade(rgb, k):
    """k > 0 lighter, k < 0 darker."""
    if k >= 0:
        return tuple(int(c + (255 - c) * k) for c in rgb[:3])
    return tuple(int(c * (1 + k)) for c in rgb[:3])


def gradient(w, h, top, bottom, alpha=255):
    """Vertical gradient RGBA image."""
    t = np.linspace(0, 1, max(1, h), dtype=np.float32)[:, None, None]
    a = np.array(top[:3], np.float32)[None, None, :]
    b = np.array(bottom[:3], np.float32)[None, None, :]
    rgb = (a + (b - a) * t) * np.ones((1, max(1, w), 1), np.float32)
    al = np.full((max(1, h), max(1, w), 1), alpha, np.float32)
    return Image.fromarray(np.concatenate([rgb, al], 2).astype(np.uint8), "RGBA")


def hgradient(w, h, left, right, a_left=255, a_right=255):
    t = np.linspace(0, 1, max(1, w), dtype=np.float32)[None, :, None]
    a = np.array(list(left[:3]) + [a_left], np.float32)[None, None, :]
    b = np.array(list(right[:3]) + [a_right], np.float32)[None, None, :]
    img = (a + (b - a) * t) * np.ones((max(1, h), 1, 1), np.float32)
    return Image.fromarray(img.astype(np.uint8), "RGBA")


def _paste_shape(im, fill_img, shape_fn):
    """Paste fill_img (same size as im) through a mask drawn by shape_fn(draw)."""
    mask = Image.new("L", im.size, 0)
    shape_fn(ImageDraw.Draw(mask))
    im.alpha_composite(Image.composite(fill_img, Image.new("RGBA", im.size, (0, 0, 0, 0)), mask))


def _shadow(im, blur, alpha=140, dy=0):
    """A soft shadow under what is drawn on im (returns the new image)."""
    a = np.asarray(im)[..., 3]
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sh.putalpha(Image.fromarray((a.astype(np.float32) * alpha / 255).astype(np.uint8)))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    if dy:
        moved = Image.new("RGBA", im.size, (0, 0, 0, 0))
        moved.paste(sh, (0, dy))
        sh = moved
    sh.alpha_composite(im)
    return sh


def _glow(im, color, blur, strength=1.6):
    """Coloured glow around what is drawn on im (neon look)."""
    a = np.asarray(im)[..., 3].astype(np.float32)
    g = np.zeros((im.height, im.width, 4), np.uint8)
    g[..., :3] = color[:3]
    g[..., 3] = np.clip(a * strength, 0, 255).astype(np.uint8)
    glow = Image.fromarray(g, "RGBA").filter(ImageFilter.GaussianBlur(blur))
    glow.alpha_composite(im)
    return glow


def _down(im):
    w, h = max(1, im.width // SS), max(1, im.height // SS)
    return im.resize((w, h), Image.LANCZOS)


def _fonts(cfg, size):
    T = _t()
    name = T._fontname(cfg)
    bold = cfg.get("bold", True)
    return (T.get_font(name, size * SS, bold), T.get_font(name, max(10, int(size * 0.62)) * SS, False),
            T.get_font(name, max(10, int(size * 0.55)) * SS, True))


def _colors(cfg):
    T = _t()
    color = T._rgb(cfg.get("color"), (255, 255, 255))
    bg = T._rgb(cfg.get("bg"), (10, 30, 80))
    accent = T._rgb(cfg.get("accent"), (224, 38, 27))
    bg_a = int(255 * max(0, min(100, T._num(cfg.get("bg_alpha"), 85))) / 100)
    return color, bg, accent, bg_a


# ---------------------------------------------------------------- lower thirds
def lower3(cfg, out_w, look):
    T = _t()
    size = int(T._num(cfg.get("size"), 44))
    f1, f2, _ = _fonts(cfg, size)
    color, bg, accent, bg_a = _colors(cfg)
    l1, l2 = cfg.get("line1") or "", cfg.get("line2") or ""
    w1, w2 = T._text_w(f1, l1), T._text_w(f2, l2)
    s = size * SS
    pad = max(8, s // 3)
    fixed = int(T._num(cfg.get("width"), 0)) * SS
    h1 = int(s * 1.4)
    h2 = int(s * 0.62 * 1.55) if l2 else 0
    cap = out_w * SS

    if look == "news":
        slant = h1 // 2
        bw1 = min(cap, fixed or w1 + pad * 3 + slant)
        bw2 = min(cap, fixed or w2 + pad * 3 + slant) if l2 else 0
        W = max(bw1, bw2 + slant // 2) + slant + 8
        H = h1 + h2 + 8 * SS
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        top = gradient(W, H, _shade(accent, 0.18), _shade(accent, -0.25))
        _paste_shape(im, top, lambda d: d.polygon([(0, 0), (bw1 + slant, 0), (bw1, h1), (0, h1)], fill=255))
        d = ImageDraw.Draw(im)
        d.line([(0, 2 * SS), (bw1 + slant - 2 * SS, 2 * SS)], fill=_shade(accent, 0.45) + (200,), width=2 * SS)
        d.text((pad, h1 // 2), l1, font=f1, fill=ink(accent, color), anchor="lm")
        if l2:
            bar = gradient(W, H, _shade(bg, 0.08), _shade(bg, -0.12), bg_a or 255)
            y0 = h1
            _paste_shape(im, bar, lambda d: d.polygon([(0, y0), (bw2 + slant // 2, y0), (bw2, y0 + h2), (0, y0 + h2)], fill=255))
            d.text((pad, y0 + h2 // 2), l2, font=f2, fill=ink(bg, color), anchor="lm")
        return _down(_shadow(im, 6 * SS, 120, 3 * SS))

    if look == "glass":
        W = min(cap, fixed or max(w1, w2) + pad * 4 + 14 * SS)
        H = h1 + h2 + pad
        r = 14 * SS
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        panel = gradient(W, H, _shade(bg, 0.25), _shade(bg, -0.35), max(140, bg_a))
        _paste_shape(im, panel, lambda d: d.rounded_rectangle([0, 0, W - 1, H - 1], r, fill=255))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, W - 1, H - 1], r, outline=(255, 255, 255, 60), width=2 * SS)
        d.line([(r, 2 * SS), (W - r, 2 * SS)], fill=(255, 255, 255, 90), width=SS)          # top shine
        d.rounded_rectangle([pad // 2, pad // 2, pad // 2 + 7 * SS, H - pad // 2], 4 * SS, fill=accent + (255,))
        x = pad // 2 + 7 * SS + pad
        d.text((x, h1 // 2 + pad // 3), l1, font=f1, fill=color + (255,), anchor="lm")
        if l2:
            d.line([(x, h1 + pad // 4), (x + max(w1, w2) * 0.35, h1 + pad // 4)], fill=accent + (255,), width=3 * SS)
            d.text((x, h1 + pad // 3 + h2 // 2), l2, font=f2, fill=_shade(color, -0.12) + (255,), anchor="lm")
        return _down(_shadow(im, 10 * SS, 150, 4 * SS))

    if look == "sports":
        if cfg.get("upper", True):
            l1 = l1.upper()
            w1 = T._text_w(f1, l1)
        sk = h1 // 3
        tag = h1 // 2
        bw1 = min(cap, fixed or w1 + pad * 3)
        W = tag + bw1 + sk * 3
        H = h1 + h2 + 6 * SS
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _paste_shape(im, gradient(W, H, _shade(accent, 0.2), _shade(accent, -0.3)),
                     lambda d: d.polygon([(sk, 0), (sk + tag, 0), (tag, h1), (0, h1)], fill=255))
        x0 = tag + 4 * SS
        _paste_shape(im, gradient(W, H, _shade(bg, 0.15), _shade(bg, -0.3), max(200, bg_a)),
                     lambda d: d.polygon([(x0 + sk, 0), (x0 + sk + bw1, 0), (x0 + bw1, h1), (x0, h1)], fill=255))
        d = ImageDraw.Draw(im)
        d.text((x0 + sk // 2 + pad, h1 // 2), l1, font=f1,
               fill=ink(bg, color), anchor="lm")
        if l2:
            bw2 = min(W - x0, w2 + pad * 3)
            y0 = h1 + 3 * SS
            _paste_shape(im, Image.new("RGBA", (W, H), (245, 245, 245, 255)),
                         lambda d: d.polygon([(x0 + sk // 2, y0), (x0 + sk // 2 + bw2, y0), (x0 + bw2, y0 + h2), (x0, y0 + h2)], fill=255))
            d.text((x0 + sk // 2 + pad, y0 + h2 // 2), l2, font=f2, fill=ink((245, 245, 245), color), anchor="lm")
        return _down(_shadow(im, 5 * SS, 130, 3 * SS))

    if look == "minimal":
        W = min(cap, max(w1, w2) + pad * 2 + 20 * SS)
        H = h1 + h2 + pad * 2
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.text((pad, pad + h1 // 2), l1, font=f1, fill=color + (255,), anchor="lm")
        if l2:
            d.rectangle([pad, pad + h1 - 2 * SS, pad + max(60 * SS, int(w1 * 0.25)), pad + h1 + 2 * SS], fill=accent + (255,))
            d.text((pad, pad + h1 + 4 * SS + h2 // 2), l2, font=f2, fill=_shade(color, -0.08) + (255,), anchor="lm")
        return _down(_shadow(im, 4 * SS, 220, 2 * SS))

    if look == "elegant":
        gold = accent
        tw = max(w1, w2)
        W = min(cap, fixed or tw + pad * 8)
        H = h1 + h2 + pad * 2
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        im.alpha_composite(_fade_band(W, H, bg, bg_a))
        d = ImageDraw.Draw(im)
        line = hgradient(W, 2 * SS, gold, gold, 0, 0)
        arr = np.asarray(line).copy()
        mid = W // 2
        ramp = np.clip(1 - np.abs(np.arange(W) - mid) / (W * 0.45), 0, 1)
        arr[..., 3] = (ramp * 255).astype(np.uint8)[None, :]
        band = Image.fromarray(arr, "RGBA")
        im.alpha_composite(band, (0, 3 * SS))
        im.alpha_composite(band, (0, H - 5 * SS))
        d.text((W // 2, pad + h1 // 2), l1, font=f1, fill=color + (255,), anchor="mm")
        if l2:
            d.text((W // 2, pad + h1 + h2 // 2), l2, font=f2, fill=gold + (255,), anchor="mm")
        return _down(_shadow(im, 4 * SS, 120, 2 * SS))

    if look == "pill":
        bw1 = min(cap, fixed or w1 + pad * 4)
        W = max(bw1, (w2 + pad * 4 + h1 // 3) if l2 else 0) + 8 * SS
        H = h1 + (h2 + 6 * SS if l2 else 0) + 6 * SS
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _paste_shape(im, gradient(W, H, _shade(bg, 0.15), _shade(bg, -0.25), max(200, bg_a)),
                     lambda d: d.rounded_rectangle([0, 0, bw1, h1], h1 // 2, fill=255))
        d = ImageDraw.Draw(im)
        d.ellipse([h1 // 4, h1 // 2 - 6 * SS, h1 // 4 + 12 * SS, h1 // 2 + 6 * SS], fill=accent + (255,))
        d.text((h1 // 4 + 12 * SS + pad // 2, h1 // 2), l1, font=f1, fill=ink(bg, color), anchor="lm")
        if l2:
            x0, y0 = h1 // 3, h1 + 6 * SS
            bw2 = w2 + pad * 3
            _paste_shape(im, gradient(W, H, _shade(accent, 0.15), _shade(accent, -0.2)),
                         lambda d: d.rounded_rectangle([x0, y0, x0 + bw2, y0 + h2], h2 // 2, fill=255))
            d.text((x0 + pad * 1.5, y0 + h2 // 2), l2, font=f2, fill=ink(accent, color), anchor="lm")
        return _down(_shadow(im, 6 * SS, 130, 3 * SS))
    if look == "neon":
        W = min(cap, fixed or max(w1, w2) + pad * 4)
        H = h1 + h2 + pad
        m = 16 * SS                                  # room for the glow
        im = Image.new("RGBA", (W + m * 2, H + m * 2), (0, 0, 0, 0))
        _paste_shape(im, gradient(im.width, im.height, _shade(bg, 0.05), _shade(bg, -0.4), max(170, bg_a)),
                     lambda d: d.rounded_rectangle([m, m, m + W, m + H], 10 * SS, fill=255))
        line = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(line).rounded_rectangle([m, m, m + W, m + H], 10 * SS, outline=accent + (255,), width=3 * SS)
        im.alpha_composite(_glow(line, accent, 7 * SS))
        d = ImageDraw.Draw(im)
        d.text((m + pad * 1.5, m + h1 // 2 + pad // 3), l1, font=f1, fill=color + (255,), anchor="lm")
        if l2:
            d.text((m + pad * 1.5, m + h1 + h2 // 2), l2, font=f2, fill=_shade(accent, 0.35) + (255,), anchor="lm")
        return _down(im)

    if look == "split":
        blk = h1 + h2
        bw = min(cap, fixed or max(w1, w2) + pad * 4)
        W, H = blk + bw, blk + 4 * SS
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _paste_shape(im, gradient(W, H, _shade(accent, 0.2), _shade(accent, -0.25)),
                     lambda d: d.rectangle([0, 0, blk, blk], fill=255))
        d = ImageDraw.Draw(im)
        initial = (l1.strip()[:1] or "").upper()
        if initial:
            fi = _t().get_font(_t()._fontname(cfg), int(blk * 0.55), True)
            d.text((blk // 2, blk // 2), initial, font=fi, fill=ink(accent, (255, 255, 255)), anchor="mm")
        card = (250, 250, 250)
        _paste_shape(im, gradient(W, H, card, (232, 234, 238), max(220, bg_a)),
                     lambda dd: dd.rectangle([blk, 0, W, blk], fill=255))
        d.rectangle([blk, blk, W, blk + 4 * SS], fill=bg + (255,))
        d.text((blk + pad, h1 // 2 + (0 if l2 else h2 // 2)), l1, font=f1, fill=(20, 22, 28, 255), anchor="lm")
        if l2:
            d.text((blk + pad, h1 + h2 // 2 - pad // 4), l2, font=f2, fill=_shade(bg, -0.1) + (255,), anchor="lm")
        return _down(_shadow(im, 8 * SS, 120, 4 * SS))

    if look == "gradient":
        W = min(cap, fixed or max(w1, w2) + pad * 3 + (h1 + h2) // 2)
        H = h1 + h2 + pad
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _paste_shape(im, hgradient(W, H, accent, bg, 255, max(0, bg_a - 60)),
                     lambda d: d.rounded_rectangle([-H, 0, W - 1, H - 1], H // 2, fill=255))
        d = ImageDraw.Draw(im)
        d.text((pad, h1 // 2 + pad // 3), l1, font=f1, fill=ink(accent, color), anchor="lm")
        if l2:
            d.text((pad, h1 + h2 // 2), l2, font=f2, fill=ink(accent, color)[:3] + (220,), anchor="lm")
        return _down(_shadow(im, 8 * SS, 120, 4 * SS))

    if look == "frame":
        W = min(cap, fixed or max(w1, w2) + pad * 4)
        H = h1 + h2 + pad * 2
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if bg_a:
            im.alpha_composite(Image.new("RGBA", (W, H), bg + (min(bg_a, 140),)))
        d = ImageDraw.Draw(im)
        c, t = min(W, H) // 3, 4 * SS
        for (x, y, dx, dy) in ((0, 0, 1, 1), (W - 1, 0, -1, 1), (0, H - 1, 1, -1), (W - 1, H - 1, -1, -1)):
            d.line([(x, y), (x + dx * c, y)], fill=accent + (255,), width=t)
            d.line([(x, y), (x, y + dy * c)], fill=accent + (255,), width=t)
        d.text((pad * 2, pad + h1 // 2), l1, font=f1, fill=color + (255,), anchor="lm")
        if l2:
            d.text((pad * 2, pad + h1 + h2 // 2), l2, font=f2, fill=_shade(accent, 0.3) + (255,), anchor="lm")
        return _down(_shadow(im, 4 * SS, 160, 2 * SS))
    return None


def _fade_band(W, H, bg, bg_a):
    """Dark band that fades out to both sides (elegant look)."""
    a = max(0, min(255, bg_a))
    x = np.linspace(-1, 1, W)
    ramp = np.clip(1.25 - np.abs(x) * 1.25, 0, 1) ** 0.7
    arr = np.zeros((H, W, 4), np.uint8)
    arr[..., :3] = bg[:3]
    arr[..., 3] = (ramp * a)[None, :].astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


# ---------------------------------------------------------------- text box / clock / countdown
def box(cfg, text, out_w, look):
    T = _t()
    size = int(T._num(cfg.get("size"), 44))
    f1 = T.get_font(T._fontname(cfg), size * SS, cfg.get("bold", True))
    color, bg, accent, bg_a = _colors(cfg)
    s = size * SS
    pad = max(8, s // 3)
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    b = tmp.multiline_textbbox((0, 0), text, font=f1, spacing=s // 4)
    tw, th = b[2] - b[0], b[3] - b[1]
    fixed = int(T._num(cfg.get("width"), 0)) * SS
    extra = {"pill": th // 2 + pad, "news": 10 * SS, "split": 10 * SS, "neon": pad, "frame": pad, "sports": (th + pad * 2) // 4 + 10 * SS}.get(look, 0)
    W = min(out_w * SS, fixed or tw + pad * 2 + extra)
    H = th + pad * 2
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    tx_color = color + (255,)
    if look == "glass":
        r = 12 * SS
        _paste_shape(im, gradient(W, H, _shade(bg, 0.25), _shade(bg, -0.35), max(140, bg_a)),
                     lambda dd: dd.rounded_rectangle([0, 0, W - 1, H - 1], r, fill=255))
        d.rounded_rectangle([0, 0, W - 1, H - 1], r, outline=(255, 255, 255, 70), width=2 * SS)
        d.rectangle([r, H - 4 * SS, W - r, H - 2 * SS], fill=accent + (255,))
    elif look == "pill":
        _paste_shape(im, gradient(W, H, _shade(bg, 0.15), _shade(bg, -0.25), max(200, bg_a)),
                     lambda dd: dd.rounded_rectangle([0, 0, W - 1, H - 1], H // 2, fill=255))
        tx_color = ink(bg, color)
    elif look == "news":
        _paste_shape(im, gradient(W, H, _shade(bg, 0.1), _shade(bg, -0.2), max(200, bg_a)),
                     lambda dd: dd.rectangle([0, 0, W, H], fill=255))
        d.rectangle([0, 0, 10 * SS, H], fill=accent + (255,))
        tx_color = ink(bg, color)
    elif look == "sports":
        sk = H // 4
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        _paste_shape(im, gradient(W, H, _shade(bg, 0.15), _shade(bg, -0.3), max(200, bg_a)),
                     lambda dd: dd.polygon([(sk, 0), (W, 0), (W - sk, H), (0, H)], fill=255))
        d.polygon([(sk, 0), (sk + 8 * SS, 0), (8 * SS, H), (0, H)], fill=accent + (255,))
        tx_color = ink(bg, color)
    elif look == "elegant":
        im.alpha_composite(_fade_band(W, H, bg, bg_a))
        d.line([(W * 0.15, 2 * SS), (W * 0.85, 2 * SS)], fill=accent + (255,), width=2 * SS)
        d.line([(W * 0.15, H - 3 * SS), (W * 0.85, H - 3 * SS)], fill=accent + (255,), width=2 * SS)
    elif look in ("neon", "frame", "gradient", "split"):
        if look == "gradient":
            _paste_shape(im, hgradient(W, H, accent, bg, 255, max(0, bg_a - 60)),
                         lambda dd: dd.rounded_rectangle([-H, 0, W - 1, H - 1], H // 2, fill=255))
            tx_color = ink(accent, color)
        elif look == "split":
            _paste_shape(im, gradient(W, H, (250, 250, 250), (232, 234, 238), max(220, bg_a)),
                         lambda dd: dd.rectangle([0, 0, W, H], fill=255))
            d.rectangle([0, 0, 8 * SS, H], fill=accent + (255,))
            tx_color = (20, 22, 28, 255)
        else:
            if bg_a:
                _paste_shape(im, gradient(W, H, _shade(bg, 0.05), _shade(bg, -0.4), max(170, bg_a) if look == "neon" else min(bg_a, 140)),
                             lambda dd: dd.rounded_rectangle([0, 0, W - 1, H - 1], 8 * SS if look == "neon" else 0, fill=255))
            line = Image.new("RGBA", im.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(line)
            if look == "neon":
                ld.rounded_rectangle([2 * SS, 2 * SS, W - 2 * SS, H - 2 * SS], 8 * SS, outline=accent + (255,), width=3 * SS)
                im.alpha_composite(_glow(line, accent, 4 * SS))
            else:
                c, t = min(W, H) // 3, 4 * SS
                for (x, y, dx, dy) in ((0, 0, 1, 1), (W - 1, 0, -1, 1), (0, H - 1, 1, -1), (W - 1, H - 1, -1, -1)):
                    ld.line([(x, y), (x + dx * c, y)], fill=accent + (255,), width=t)
                    ld.line([(x, y), (x, y + dy * c)], fill=accent + (255,), width=t)
                im.alpha_composite(line)
    # minimal: no box
    centered = look in ("elegant", "pill", "neon", "frame") or bool(cfg.get("width"))
    x = (W - tw) // 2 - b[0] if centered else pad + extra // (2 if look == "pill" else 1) - b[0]
    d.text((x, pad - b[1]), text, font=f1, fill=tx_color, spacing=s // 4, align="center" if centered else "left")
    return _down(_shadow(im, (4 if look == "minimal" else 6) * SS, 220 if look == "minimal" else 120, 2 * SS))


# ---------------------------------------------------------------- ticker
def ticker_base(cfg, box_w, h, label, label_font):
    """The ticker bar and its label (the text runs over it). Returns (PIL image, label width)."""
    T = _t()
    look = cfg.get("look") or "classic"
    color, bg, accent, _ = _colors(cfg)
    bg_a = int(255 * max(0, min(100, T._num(cfg.get("bg_alpha"), 90))) / 100)
    W, H = box_w * SS, h * SS
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if look == "news":
        im.alpha_composite(gradient(W, H, _shade(bg, 0.12), _shade(bg, -0.3), bg_a))
        d.rectangle([0, 0, W, 3 * SS], fill=accent + (255,))
    elif look == "glass":
        im.alpha_composite(gradient(W, H, _shade(bg, 0.3), _shade(bg, -0.4), max(150, bg_a)))
        d.line([(0, SS), (W, SS)], fill=(255, 255, 255, 80), width=SS)
    elif look == "neon":
        im.alpha_composite(gradient(W, H, _shade(bg, 0.05), _shade(bg, -0.45), max(180, bg_a)))
        line = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(line).rectangle([0, 0, W, 2 * SS], fill=accent + (255,))
        im.alpha_composite(_glow(line, accent, 3 * SS, 2.5))
    elif look == "gradient":
        im.alpha_composite(hgradient(W, H, _shade(accent, -0.35), bg, max(200, bg_a), bg_a))
    elif look == "minimal":
        im.alpha_composite(gradient(W, H, (0, 0, 0), (0, 0, 0), 0))
        im.alpha_composite(gradient(W, H // 2, (0, 0, 0), (0, 0, 0), 0), (0, 0))
        arr = np.zeros((H, W, 4), np.uint8)
        arr[..., 3] = (np.linspace(0, 1, H) ** 1.5 * 170).astype(np.uint8)[:, None]
        im.alpha_composite(Image.fromarray(arr, "RGBA"))
        d.rectangle([0, 0, W, SS], fill=accent + (200,))
    else:
        im.alpha_composite(Image.new("RGBA", (W, H), bg + (bg_a,)))
    lw = 0
    if label:
        lf = label_font
        tw = T._text_w(lf, label)
        if look in ("news", "sports"):
            sl = H // 3
            lw = tw + h * SS // 2 + sl
            _paste_shape(im, gradient(W, H, _shade(accent, 0.2), _shade(accent, -0.3)),
                         lambda dd: dd.polygon([(0, 0), (lw, 0), (lw - sl, H), (0, H)], fill=255))
            d.text(((lw - sl) // 2, H // 2), label, font=lf, fill=ink(accent, color), anchor="mm")
        else:
            lw = tw + h * SS // 2
            _paste_shape(im, gradient(W, H, _shade(accent, 0.15), _shade(accent, -0.25)),
                         lambda dd: dd.rectangle([0, 0, lw, H], fill=255))
            d.text((lw // 2, H // 2), label, font=lf, fill=ink(accent, color), anchor="mm")
        lw = lw // SS + 4
    return _down(im), lw
