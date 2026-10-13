"""CG Composer: a separate window for preparing CGs.

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

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import paths
from cg import CG_EXT, list_cg
from cgedit import PRESET_EXT, make_blueprint, read_cgp, write_cgp

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
    return folder, int(c.get("width", 1920)), int(c.get("height", 1080))


FOLDER, WIDTH, HEIGHT = _settings()
app = Flask(__name__, static_folder=None)
app.register_blueprint(make_blueprint(lambda: FOLDER, lambda: (WIDTH, HEIGHT)))


@app.get("/")
def index():
    return send_from_directory(os.path.join(BUNDLE, "web"), "cgedit.html")


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
        name = secure_filename(f.filename or "")
        if name and os.path.splitext(name)[1].lower() in CG_EXT:
            f.save(os.path.join(FOLDER, name))
            saved.append(name)
    return jsonify(saved=saved)


class Api:
    """Called from the page through window.pywebview.api (desktop window only)."""

    def __init__(self):
        self.window = None

    def export_cgp(self, cfg):
        import webview
        name = secure_filename(cfg.get("name") or os.path.splitext(cfg.get("file") or "cg")[0]) or "cg"
        res = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=name + PRESET_EXT,
                                             file_types=("CG preset (*.cgp)",))
        if not res:
            return None
        dest = res if isinstance(res, str) else res[0]
        if not dest.lower().endswith(PRESET_EXT):
            dest += PRESET_EXT
        write_cgp(dest, FOLDER, cfg)
        return dest


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    port = free_port()
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False),
                     daemon=True, name="composer web").start()
    if "--browser" in sys.argv or not _have_webview():
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}/")
        threading.Event().wait()
        return 0
    import webview
    import splash
    _own_taskbar_button()
    w, h = splash.SIZE
    loading = webview.create_window("CG Composer loading", html=splash.html("composer"), width=w, height=h,
                                    frameless=True, resizable=False, on_top=True, background_color="#0c0c0e")
    api = Api()

    def boot():
        splash.status(loading, "Loading CG Composer...")
        time.sleep(1.6)                                      # let the logo be seen
        api.window = webview.create_window("CG Composer", f"http://127.0.0.1:{port}/", js_api=api,
                                           width=1280, height=900, min_size=(1000, 700), background_color="#0c0d0f")
        api.window.events.shown += lambda: threading.Timer(0.5, _set_icon).start()
        loading.destroy()

    webview.start(boot)
    return 0


def _icon_path():
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "build")
    return os.path.join(base, "cgcomposer.ico")


def _own_taskbar_button():
    """Windows: CG Composer gets its own taskbar button (not grouped under Playout)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Endora.CGComposer")
        except Exception:
            pass


def _set_icon():
    """Windows: the CG Composer logo on the window and its taskbar button."""
    if sys.platform != "win32" or not os.path.isfile(_icon_path()):
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        u.LoadImageW.restype = ctypes.c_void_p
        hwnd = u.FindWindowW(None, "CG Composer")
        if not hwnd:
            return
        for size, which in ((32, 1), (16, 0)):         # ICON_BIG, ICON_SMALL
            h = u.LoadImageW(None, _icon_path(), 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
            if h:
                u.SendMessageW(ctypes.c_void_p(hwnd), 0x80, which, ctypes.c_void_p(h))   # WM_SETICON
    except Exception:
        pass


def _have_webview():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


if __name__ == "__main__":
    os._exit(main() or 0)
