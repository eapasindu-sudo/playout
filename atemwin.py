"""ATEM & Cameras: its own window, opened from Functions > ATEM & Cameras (like CG Composer).

Shows Playout's /atem page: the ATEM Switcher's phone switcher and Sony CCU, its setup page
(ATEM IP, Sony cameras, phones) and the Playout link settings. Pressing the button again brings
the open window to the front instead of opening a second one.

    Playout.exe --atem
    python desktop.py --atem
"""
import json
import os
import sys

import paths
import winicon

TITLE = "ATEM & Cameras"


def _playout_port():
    try:
        with open(os.path.join(paths.user_home(), "config.json"), encoding="utf-8") as f:
            return int(json.load(f).get("web_port", 8080))
    except Exception:
        return 8080


def _bring_to_front():
    """True when the window is open already (it is brought to the front)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        hwnd = u.FindWindowW(None, TITLE)
        if not hwnd:
            return False
        u.ShowWindow(ctypes.c_void_p(hwnd), 9)          # SW_RESTORE
        u.SetForegroundWindow(ctypes.c_void_p(hwnd))
        return True
    except Exception:
        return False


def main():
    if _bring_to_front():
        return 0
    url = f"http://127.0.0.1:{_playout_port()}/atem"
    try:
        import webview
    except Exception:
        import webbrowser
        webbrowser.open(url)
        return 0
    winicon.own_taskbar_button("Endora.AtemCameras")
    win = webview.create_window(TITLE, url, width=1280, height=820, min_size=(900, 600), background_color="#0c0d0f")
    win.events.shown += lambda: winicon.set_icon(TITLE, "endora-playout.ico")
    webview.start()
    return 0


if __name__ == "__main__":
    os._exit(main() or 0)
