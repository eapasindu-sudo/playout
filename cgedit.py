"""CG editor service: preview renders, file info and .cgp preset files.

Shared by the playout control screen (CG setup dialog) and the separate CG Composer app.

A .cgp file is a zip with
    cg.json     {"version": 1, "cfg": {...effects, position...}, "file": "<media name>"}
    <media>     the CG picture / video itself
so one file carries everything, and Playout can load it on another PC.
"""
import io
import json
import os
import zipfile

import numpy as np
from flask import Blueprint, Response, abort, jsonify, request, send_file
from PIL import Image

from cg import CG_EXT, cg_info, cg_kind, composite
from cgfx import checker, render_still
from paths import safe_name
from squeeze import clean_box, compose, find_green

PRESET_EXT = ".cgp"
KEEP = ("name", "x", "y", "zoom", "zoom_x", "zoom_y", "rotate", "crop", "fade", "loop", "live_only",
        "delay", "duration", "key", "key_color", "key_similarity", "key_blend", "key_spill",
        "luma_threshold", "luma_softness", "squeeze", "squeeze_time")


def test_feed(W, H):
    """Stand-in for the program in a squeeze preview when Playout is not running: colour bars."""
    cols = [(192, 192, 192), (0, 192, 192), (192, 192, 0), (0, 192, 0), (192, 0, 192), (0, 0, 192), (192, 0, 0)]
    img = np.zeros((H, W, 4), np.uint8)
    img[..., 3] = 255
    for i, (r, g, b) in enumerate(cols):
        img[:, i * W // 7:(i + 1) * W // 7, :3] = (b, g, r)
    return img


def fit_16x9(box, W, H):
    """Program-shaped box that covers the green area (centred). A keyed overlay hides what is
    outside the green, so the program fills it without black edges and without stretching."""
    ar = W / H
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    if w / h > ar:
        nh = round(w / ar)
        return {"x": x, "y": y - (nh - h) // 2, "w": w, "h": nh}
    nw = round(h * ar)
    return {"x": x - (nw - w) // 2, "y": y, "w": nw, "h": h}


def clean_cfg(cfg):
    return {k: cfg[k] for k in KEEP if k in cfg}


def write_cgp(dest, folder, cfg):
    """Save a .cgp (settings + the media file) to dest."""
    name = cfg.get("file") or ""
    src = os.path.join(folder, name)
    if not os.path.isfile(src):
        raise FileNotFoundError(name)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        z.writestr("cg.json", json.dumps({"version": 1, "file": name, "cfg": clean_cfg(cfg)}, indent=1))
        z.write(src, "media/" + name)


def read_cgp(path_or_file, folder):
    """Open a .cgp: puts its media into folder (renamed if a different file has that name) -> cfg."""
    with zipfile.ZipFile(path_or_file) as z:
        meta = json.loads(z.read("cg.json"))
        name = safe_name(meta.get("file") or "")
        if not name or not cg_kind(name):
            raise ValueError("not a CG preset")
        data = z.read("media/" + meta["file"])
    target = os.path.join(folder, name)
    if os.path.isfile(target) and os.path.getsize(target) != len(data):
        base, ext = os.path.splitext(name)
        i = 2
        while os.path.isfile(os.path.join(folder, f"{base}_{i}{ext}")):
            i += 1
        name = f"{base}_{i}{ext}"
        target = os.path.join(folder, name)
    if not os.path.isfile(target):
        with open(target, "wb") as f:
            f.write(data)
    cfg = clean_cfg(meta.get("cfg") or {})
    cfg["file"] = name
    return cfg


def update_preset(path, changes):
    """Change settings inside a .cgp (its media stays as it is)."""
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read("cg.json"))
        files = {n: z.read(n) for n in z.namelist() if n != "cg.json"}
    meta["cfg"] = clean_cfg({**(meta.get("cfg") or {}), **changes})
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        z.writestr("cg.json", json.dumps(meta, indent=1))
        for n, data in files.items():
            z.writestr(n, data)
    os.replace(tmp, path)
    return meta


def list_presets(folder):
    out = []
    for n in sorted(os.listdir(folder), key=str.lower):
        if n.lower().endswith(PRESET_EXT):
            try:
                with zipfile.ZipFile(os.path.join(folder, n)) as z:
                    meta = json.loads(z.read("cg.json"))
                out.append({"name": n, "file": meta.get("file"), "cfg": meta.get("cfg") or {}})
            except Exception:
                continue
    return out


def make_blueprint(folder_fn, size_fn, background_fn=None):
    """folder_fn() -> CG folder; size_fn() -> (output w, h); background_fn() -> flat BGRA program frame or None."""
    bp = Blueprint("cgedit", __name__)

    def path_of(name):
        p = os.path.join(folder_fn(), safe_name(name or ""))
        if not os.path.isfile(p) or not cg_kind(p):
            abort(404)
        return p

    @bp.get("/api/cgfx/size")
    def size():
        W, H = size_fn()
        return jsonify(width=W, height=H)

    @bp.get("/api/cgfx/info")
    def info():
        p = path_of(request.args.get("file"))
        if not p or not os.path.isfile(p):
            return jsonify(kind=None, missing=True)
        return jsonify(kind=cg_kind(p), **cg_info(p))

    @bp.post("/api/cgfx/render")
    def render():
        body = request.get_json(silent=True) or {}
        cfg = body.get("cfg") or {}
        p = path_of(cfg.get("file"))
        W, H = size_fn()
        bg = body.get("bg", "checker")
        frame = None
        if bg == "program" and background_fn:
            frame = background_fn()
            canvas = np.array(frame, copy=True).reshape(H, W, 4) if frame is not None else checker(W, H, 24)
        elif bg == "black":
            canvas = np.zeros((H, W, 4), np.uint8)
            canvas[..., 3] = 255
        else:
            canvas = checker(W, H, 24)
        box = None
        try:
            img, (dx, dy) = render_still(p, cg_kind(p), cfg, float(body.get("t") or 0))
        except Exception as e:
            return jsonify(error=str(e)), 400
        sq = clean_box(cfg.get("squeeze") or {}, W, H) if body.get("squeeze") else None
        if sq is not None:
            # squeeze preview: the program (or a test picture) in the box, the overlay around it
            feed = canvas if frame is not None else test_feed(W, H)
            canvas = compose(feed, img, int(cfg.get("x") or 0) + dx, int(cfg.get("y") or 0) + dy, sq,
                             (cfg.get("key") or "none") != "none")
            img = None
            box = [sq["x"], sq["y"], sq["w"], sq["h"]]
        if img is not None:
            x, y = int(cfg.get("x") or 0) + dx, int(cfg.get("y") or 0) + dy
            composite(canvas, img, x, y, 255)
            box = [x, y, img.shape[1], img.shape[0]]
        out_w = max(160, min(W, int(body.get("w") or 960)))
        im = Image.fromarray(canvas[..., [2, 1, 0]])
        if out_w != W:
            im = im.resize((out_w, round(H * out_w / W)), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=1)
        r = Response(buf.getvalue(), mimetype="image/png")
        r.headers["X-CG-Box"] = json.dumps(box)
        r.headers["Cache-Control"] = "no-store"
        return r

    @bp.post("/api/cgfx/find_green")
    def find_green_api():
        """Squeeze: where the green screen area of this CG is on the output (x, y, w, h)."""
        body = request.get_json(silent=True) or {}
        cfg = dict(body.get("cfg") or {})
        p = path_of(cfg.get("file"))
        cfg["key"] = "none"
        info = cg_info(p)
        t = min(2.0, (info.get("duration") or 0) / 2)
        try:
            img, (dx, dy) = render_still(p, cg_kind(p), cfg, float(body.get("t") or t))
        except Exception as e:
            return jsonify(error=str(e)), 400
        box = find_green(img) if img is not None else None
        if box is None:
            return jsonify(error="No green screen area found in this CG"), 400
        box["x"] += int(cfg.get("x") or 0) + dx
        box["y"] += int(cfg.get("y") or 0) + dy
        W, H = size_fn()
        return jsonify(box=box, fit=fit_16x9(box, W, H))

    @bp.post("/api/cgfx/export")
    def export():
        cfg = request.get_json(silent=True) or {}
        path_of(cfg.get("file"))
        buf = io.BytesIO()
        write_cgp(buf, folder_fn(), cfg)
        buf.seek(0)
        name = safe_name((cfg.get("name") or os.path.splitext(cfg["file"])[0])) + PRESET_EXT
        return send_file(buf, mimetype="application/octet-stream", as_attachment=True, download_name=name)

    @bp.post("/api/cgfx/import")
    def import_():
        """Upload a .cgp (or a plain CG file) -> cfg ready for the editor; keeps a copy in the CG folder."""
        f = request.files.get("file")
        if not f:
            abort(400)
        name = safe_name(f.filename or "")
        folder = folder_fn()
        if name.lower().endswith(PRESET_EXT):
            data = f.read()
            try:
                cfg = read_cgp(io.BytesIO(data), folder)
            except Exception as e:
                return jsonify(error=f"Not a CG preset: {e}"), 400
            with open(os.path.join(folder, name), "wb") as out:
                out.write(data)
            return jsonify(cfg=cfg)
        if os.path.splitext(name)[1].lower() in CG_EXT:
            f.save(os.path.join(folder, name))
            return jsonify(cfg={"file": name})
        return jsonify(error="Use PNG, JPEG, MOV/MP4 or a .cgp file"), 400

    @bp.get("/api/cgfx/presets")
    def presets():
        return jsonify(list_presets(folder_fn()))

    @bp.post("/api/cgfx/preset/<path:name>")
    def open_preset(name):
        """Load a .cgp from the CG folder -> cfg (its media goes into the CG folder)."""
        p = os.path.join(folder_fn(), safe_name(name))
        if not os.path.isfile(p):
            abort(404)
        try:
            return jsonify(cfg=read_cgp(p, folder_fn()))
        except Exception as e:
            return jsonify(error=str(e)), 400

    @bp.post("/api/cgfx/save_preset")
    def save_preset():
        """Save the editor's CG as <name>.cgp in the CG folder (the Playout CG library)."""
        cfg = request.get_json(silent=True) or {}
        path_of(cfg.get("file"))
        name = safe_name(cfg.get("name") or os.path.splitext(cfg["file"])[0]) or "cg"
        dest = os.path.join(folder_fn(), name + PRESET_EXT)
        write_cgp(dest, folder_fn(), cfg)
        return jsonify(saved=os.path.basename(dest), path=dest)

    return bp
