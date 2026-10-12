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
from werkzeug.utils import secure_filename

from cg import CG_EXT, cg_info, cg_kind, composite
from cgfx import checker, render_still

PRESET_EXT = ".cgp"
KEEP = ("name", "x", "y", "zoom", "zoom_x", "zoom_y", "rotate", "crop", "fade", "loop", "live_only",
        "delay", "duration", "key", "key_color", "key_similarity", "key_blend", "key_spill",
        "luma_threshold", "luma_softness")


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
        name = secure_filename(meta.get("file") or "")
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
        p = os.path.join(folder_fn(), secure_filename(name or ""))
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
        return jsonify(kind=cg_kind(p), **cg_info(p))

    @bp.post("/api/cgfx/render")
    def render():
        body = request.get_json(silent=True) or {}
        cfg = body.get("cfg") or {}
        p = path_of(cfg.get("file"))
        W, H = size_fn()
        bg = body.get("bg", "checker")
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

    @bp.post("/api/cgfx/export")
    def export():
        cfg = request.get_json(silent=True) or {}
        path_of(cfg.get("file"))
        buf = io.BytesIO()
        write_cgp(buf, folder_fn(), cfg)
        buf.seek(0)
        name = secure_filename((cfg.get("name") or os.path.splitext(cfg["file"])[0])) + PRESET_EXT
        return send_file(buf, mimetype="application/octet-stream", as_attachment=True, download_name=name)

    @bp.post("/api/cgfx/import")
    def import_():
        """Upload a .cgp (or a plain CG file) -> cfg ready for the editor; keeps a copy in the CG folder."""
        f = request.files.get("file")
        if not f:
            abort(400)
        name = secure_filename(f.filename or "")
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
        p = os.path.join(folder_fn(), secure_filename(name))
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
        name = secure_filename(cfg.get("name") or os.path.splitext(cfg["file"])[0]) or "cg"
        dest = os.path.join(folder_fn(), name + PRESET_EXT)
        write_cgp(dest, folder_fn(), cfg)
        return jsonify(saved=os.path.basename(dest), path=dest)

    return bp
