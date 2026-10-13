"""Windows: the Endora logos on the app windows and taskbar buttons (not the default program icon)."""
import os
import sys


def icon_path(name):
    """build/<name> from source, or next to the bundled files in Playout.exe."""
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "build")
    return os.path.join(base, name)


def own_taskbar_button(app_id):
    """Its own taskbar button and icon (CG Composer is not grouped under Playout)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass


def set_icon(title, ico):
    """Put ico on the window with this title (window corner, Alt+Tab and taskbar)."""
    path = icon_path(ico)
    if sys.platform != "win32" or not os.path.isfile(path):
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        u.FindWindowW.restype = ctypes.c_void_p
        u.LoadImageW.restype = ctypes.c_void_p
        hwnd = u.FindWindowW(None, title)
        if not hwnd:
            return
        for size, which in ((32, 1), (16, 0)):         # ICON_BIG, ICON_SMALL
            h = u.LoadImageW(None, path, 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
            if h:
                u.SendMessageW(ctypes.c_void_p(hwnd), 0x80, which, ctypes.c_void_p(h))   # WM_SETICON
    except Exception:
        pass
