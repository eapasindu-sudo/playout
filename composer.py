"""CG Composer: a separate window for preparing CGs.

Designer (/designer, web/designer.html): lower thirds, titles, scoreboards and other CGs made from text,
shapes and pictures on one canvas, from starter templates; saved as a .cgp design that Playout plays as a
text CG whose fields (names, scores) the operator types on air (see design.py).

Open a PNG / JPEG / MOV / MP4 (also on a green or blue screen), key it, crop, zoom, rotate and
place it, then save it as a .cgp file. "Save to Playout" puts the .cgp straight into the Playout
CG library (Documents\\Playout\\cg); "Export .cgp" saves it anywhere, e.g. to copy to another PC.

    Playout.exe --composer      (the "CG Composer" shortcut)
    python composer.py          (from source)
"""
import json
import os
import socket
import sys
import threading
import time

from flask import Flask, Response, abort, jsonify, request, send_from_directory

import cglive
import design
import paths
import textcg
import winicon
from cg import CG_EXT, list_cg
from cgedit import PRESET_EXT, make_blueprint, read_cgp, write_cgp
from paths import safe_name

HOME = paths.user_home()
BUNDLE = paths.bundle_dir()


def _settings():
    """CG folder and output size from Playout's config.json (so positions match the playout output)."""
    try:
        with open(os.path.join(HOME, "config.json"), encoding="utf-8") as f:
            c = json.load(f)
    except Exception:
        c = {}
    folder = os.path.join(HOME, c.get("cg_dir", "cg"))
    os.makedirs(folder, exist_ok=True)
    return (folder, int(c.get("width", 1920)), int(c.get("height", 1080)), int(c.get("web_port", 8080)),
            int(c.get("composer_port", 8090)))


FOLDER, WIDTH, HEIGHT, PLAYOUT_PORT, COMPOSER_PORT = _settings()
textcg.set_font_dirs(FOLDER)      # fonts dropped into the CG folder
design.set_image_dirs(FOLDER)     # picture layers of Designer designs
LIVE = None                       # cglive.Live, made when the app starts (it opens the NDI output)


def live():
    global LIVE
    if LIVE is None:
        LIVE = cglive.Live(HOME, FOLDER, PLAYOUT_PORT)
    return LIVE


def program_frame():
    """The program output of Playout (running on this PC), so CGs can be lined up over the real
    picture. None when Playout is not running."""
    import io
    import urllib.request
    import numpy as np
    from PIL import Image
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PLAYOUT_PORT}/api/preview.jpg?w={WIDTH}", timeout=2) as r:
            img = Image.open(io.BytesIO(r.read())).convert("RGB")
    except Exception:
        return None
    if img.size != (WIDTH, HEIGHT):
        img = img.resize((WIDTH, HEIGHT), Image.BILINEAR)
    a = np.asarray(img)
    out = np.empty((HEIGHT, WIDTH, 4), np.uint8)
    out[..., 0], out[..., 1], out[..., 2], out[..., 3] = a[..., 2], a[..., 1], a[..., 0], 255
    return out.reshape(-1)


app = Flask(__name__, static_folder=None)
app.register_blueprint(make_blueprint(lambda: FOLDER, lambda: (WIDTH, HEIGHT), program_frame))


@app.get("/")
def index():
    return send_from_directory(os.path.join(BUNDLE, "web"), "cgedit.html")


@app.get("/designer")
def designer():
    return send_from_directory(os.path.join(BUNDLE, "web"), "designer.html")


@app.get("/img/<any('logo-playout.png', 'logo-composer.png'):name>")
def logo_img(name):
    return send_from_directory(os.path.join(BUNDLE, "web"), name, max_age=86400)


@app.get("/api/cg/files")
def files():
    return jsonify(list_cg(FOLDER))


@app.post("/api/cg/files")
def upload():
    saved = []
    for f in request.files.getlist("files"):
        name = safe_name(f.filename or "")
        if name and os.path.splitext(name)[1].lower() in CG_EXT:
            f.save(os.path.join(FOLDER, name))
            saved.append(name)
    return jsonify(saved=saved)


# ---------------------------------------------------------------- Live (playlists to air)
# Other PCs on the network may only reach the overlay (OBS / vMix Browser Source), the HTTP triggers
# (Stream Deck, Companion, vMix scripts) and the read-only state; the editor stays on this PC.
REMOTE_OK = ("/overlay", "/trigger/", "/api/live/overlay", "/api/live/stream", "/api/live/layer/", "/api/live/state",
             "/api/live/preview/")


@app.before_request
def only_local():
    if request.remote_addr not in ("127.0.0.1", "::1") and not request.path.startswith(REMOTE_OK):
        abort(403)


@app.get("/live")
def live_page():
    return send_from_directory(os.path.join(BUNDLE, "web"), "cglive.html")


@app.get("/overlay")
def overlay_page():
    r = send_from_directory(os.path.join(BUNDLE, "web"), "overlay.html")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/api/live/state")
def live_state():
    st = live().state()
    st["port"] = PORT
    st["ips"] = lan_ips()
    return jsonify(st)


@app.get("/api/live/overlay")
def live_overlay():
    return jsonify(live().overlay_state())


@app.get("/api/live/stream")
def live_stream():
    """Server-sent events: the overlay state each time it changes (and every few seconds, to keep alive)."""
    L = live()

    def gen():
        v = None
        while True:
            v = L.wait_change(v if v is not None else -1, 5)
            yield "data: " + json.dumps(L.overlay_state()) + "\n\n"

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/live/layer/<pid>/<int:i>.png")
def live_layer(pid, i):
    data = live().layer_png(pid, i)
    if data is None:
        abort(404)
    r = Response(data, mimetype="image/png")
    r.headers["Cache-Control"] = "max-age=3600"     # a design on air never changes its pictures (an update is a new id)
    return r


_PREVIEWS = {}      # (item id, rev, w, h, picture width) -> PNG


@app.get("/api/live/preview/<item_id>.png")
def live_preview(item_id):
    """An item as it looks on air (the whole frame, see-through), for the Preview monitor and the list."""
    from PIL import Image
    import io
    L = live()
    _pl, it = L._find(item_id)
    if it is None:
        abort(404)
    w, h = L.size
    tw = max(80, min(w, int(request.args.get("w") or 640)))
    key = (item_id, int(it.get("rev") or 0), w, h, tw)
    data = _PREVIEWS.get(key)
    if data is None:
        a = L._animator(it)                  # the same layers Take uses (made once for both)
        im = a.compose(a.state("on", 0))
        im = im.resize((tw, max(1, round(h * tw / w))), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=1)
        data = _PREVIEWS[key] = buf.getvalue()
        while len(_PREVIEWS) > 200:
            _PREVIEWS.pop(next(iter(_PREVIEWS)))
    r = Response(data, mimetype="image/png")
    r.headers["Cache-Control"] = "max-age=86400"     # the page asks with the item's rev, which changes with it
    return r


@app.post("/api/live/add")
def live_add():
    """Designs from the CG library (names of .cgp files) or the Designer (cfgs) into a playlist."""
    from cgedit import clean_design, is_design
    b = request.get_json(force=True, silent=True) or {}
    cfgs = [clean_design(c) for c in b.get("cfgs") or [] if is_design(c)]
    for n in b.get("names") or []:
        try:
            c = read_cgp(os.path.join(FOLDER, safe_name(n)), FOLDER)
        except Exception:
            continue
        if is_design(c):
            c["name"] = c.get("name") or os.path.splitext(n)[0]
            cfgs.append(c)
    if not cfgs:
        return jsonify(error="No designs to add."), 400
    return jsonify(added=live().add_items(cfgs, b.get("pid")))


@app.post("/api/live/playlist")
def live_playlist():
    b = request.get_json(force=True, silent=True) or {}
    live().playlist(b.get("op"), b.get("pid"), b.get("name"), b.get("order"))
    return jsonify(ok=True)


@app.post("/api/live/item")
def live_item():
    b = request.get_json(force=True, silent=True) or {}
    err = live().item(b.get("op"), b.get("id"), b.get("changes"))
    return (jsonify(error=err), 400) if err else jsonify(ok=True)


@app.post("/api/live/<any(cue, take, clear, next, prev):action>")
def live_action(action):
    b = request.get_json(force=True, silent=True) or {}
    L = live()
    if action == "cue":
        err = "" if L.cue(b.get("id")) else "item not found"
    elif action == "take":
        err = L.take(b.get("id"))
    elif action == "clear":
        err = L.clear() or ""
    elif action == "next":
        err = L.next(b.get("take", True))
    else:
        err = L.prev(b.get("take", True))
    return (jsonify(error=err), 400) if err else jsonify(ok=True)


@app.post("/api/live/outputs")
def live_outputs():
    b = request.get_json(force=True, silent=True) or {}
    live().outputs(b.get("res"), b.get("fps"), b.get("ndi_on"), b.get("ndi_name"), b.get("playout"))
    if b.get("playout"):
        live().link_playout()
    return jsonify(live().state())


@app.post("/api/live/transition")
def live_transition():
    b = request.get_json(force=True, silent=True) or {}
    live().transition(b.get("type"), b.get("dur"))
    return jsonify(ok=True)


@app.get("/api/live/package")
def live_package_get():
    """The playlists (and their pictures) as a .cgpack download (the browser; the window uses a save dialog)."""
    import tempfile
    pids = [p for p in (request.args.get("pids") or "").split(",") if p]
    fd, tmp = tempfile.mkstemp(suffix=cglive.PACKAGE_EXT)
    os.close(fd)
    live().export_package(tmp, pids)
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    r = Response(data, mimetype="application/zip")
    r.headers["Content-Disposition"] = 'attachment; filename="CG Composer' + cglive.PACKAGE_EXT + '"'
    return r


@app.post("/api/live/package")
def live_package_put():
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file."), 400
    try:
        import io
        names = live().import_package(io.BytesIO(f.read()))
    except Exception as e:
        return jsonify(error=f"Not a CG Composer package ({e})."), 400
    return jsonify(playlists=names)


@app.route("/trigger/<action>", methods=["GET", "POST"])
@app.route("/trigger/<action>/<arg>", methods=["GET", "POST"])
def trigger(action, arg=None):
    """One URL per button, for Stream Deck (Companion "HTTP request"), vMix scripts, a phone browser:
    take, clear, next, prev, cue-next, cue-prev, item/<n> (takes item n), cue/<n>,
    field/<name>?value=... (a field of the design on air, e.g. a score), playlist/<n>."""
    L = live()
    n = int(arg) if arg and arg.isdigit() else None
    if action == "take":
        err = L.take()
    elif action == "clear":
        err = L.clear() or ""
    elif action == "next":
        err = L.next()
    elif action == "prev":
        err = L.prev()
    elif action == "cue-next":
        err = L.next(take=False)
    elif action == "cue-prev":
        err = L.prev(take=False)
    elif action == "item" and n:
        err = L.by_number(n)
    elif action == "cue" and n:
        err = L.by_number(n, take=False)
    elif action == "field" and arg:
        err = L.set_field(arg, request.values.get("value", ""))
    elif action == "playlist" and n:
        pls = L.state()["playlists"]
        err = "" if 1 <= n <= len(pls) else f"no playlist {n}"
        if not err:
            L.playlist("select", pls[n - 1]["id"])
    else:
        return jsonify(ok=False, error="unknown trigger"), 404
    return (jsonify(ok=False, error=err), 400) if err else jsonify(ok=True)


def lan_ips():
    """This PC's addresses on the network (for the overlay / trigger URLs other PCs use)."""
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except Exception:
        pass
    try:
        for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(a[4][0])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


class Api:
    """Called from the page through window.pywebview.api (desktop window only)."""

    def __init__(self):
        self._window = None      # underscore: pywebview must not walk into the window object (it freezes)
        self._outputs = []

    def export_cgp(self, cfg):
        import webview
        name = safe_name(cfg.get("name") or os.path.splitext(cfg.get("file") or "cg")[0]) or "cg"
        res = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=name + PRESET_EXT,
                                             file_types=("CG preset (*.cgp)",))
        if not res:
            return None
        dest = res if isinstance(res, str) else res[0]
        if not dest.lower().endswith(PRESET_EXT):
            dest += PRESET_EXT
        write_cgp(dest, FOLDER, cfg)
        return dest

    def export_package(self, pids=None):
        import webview
        res = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename="CG Composer" + cglive.PACKAGE_EXT,
                                             file_types=("CG Composer package (*.cgpack)",))
        if not res:
            return None
        dest = res if isinstance(res, str) else res[0]
        if not dest.lower().endswith(cglive.PACKAGE_EXT):
            dest += cglive.PACKAGE_EXT
        return live().export_package(dest, pids)

    def output_window(self, bg="#000000", screen=-1):
        """The Live output full screen (on the last screen: the projector / LED / second monitor)."""
        import webview
        from urllib.parse import quote
        screens = webview.screens or []
        scr = screens[screen] if screens and -len(screens) <= screen < len(screens) else None
        kw = {"screen": scr} if scr is not None else {}
        win = webview.create_window("CG Composer output", f"http://127.0.0.1:{PORT}/overlay?bg={quote(bg)}&cursor=0",
                                    fullscreen=True, background_color=bg if bg.startswith("#") else "#000000", **kw)
        self._outputs.append(win)
        return len(screens)

    def close_outputs(self):
        for w in self._outputs:
            try:
                w.destroy()
            except Exception:
                pass
        self._outputs = []


PORT = COMPOSER_PORT


def pick_port():
    """The fixed port (composer_port in config.json, 8090) on every network card, so OBS / vMix / Stream Deck
    on other PCs keep one address; any free port on this PC alone when it is taken."""
    try:
        with socket.socket() as s:
            s.bind(("0.0.0.0", COMPOSER_PORT))
        return "0.0.0.0", COMPOSER_PORT
    except OSError:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return "127.0.0.1", s.getsockname()[1]


def main():
    global PORT
    host, PORT = pick_port()
    port = PORT
    if live().s.get("playout"):
        live().link_playout()                 # ready before the first Take (a new NDI source takes a moment)
    threading.Thread(target=lambda: app.run(host=host, port=port, threaded=True, use_reloader=False),
                     daemon=True, name="composer web").start()
    if "--browser" in sys.argv or not _have_webview():
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}/")
        threading.Event().wait()
        return 0
    import webview
    import splash
    winicon.own_taskbar_button("Endora.CGComposer")
    w, h = splash.SIZE
    loading = webview.create_window("CG Composer loading", html=splash.html("composer"), width=w, height=h,
                                    frameless=True, resizable=False, on_top=True, background_color="#0c0c0e")
    api = Api()

    def boot():
        splash.status(loading, "Loading CG Composer...")
        time.sleep(1.6)                                      # let the logo be seen
        api._window = webview.create_window("CG Composer", f"http://127.0.0.1:{port}/", js_api=api,
                                           width=1280, height=900, min_size=(1000, 700), background_color="#0c0d0f")
        api._window.events.shown += lambda: threading.Timer(0.5, winicon.set_icon, ("CG Composer", "endora-cgcomposer.ico")).start()
        loading.destroy()

    webview.start(boot)
    live().shutdown()
    return 0



def _have_webview():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


if __name__ == "__main__":
    os._exit(main() or 0)
