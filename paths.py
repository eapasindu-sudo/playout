"""Where Playout keeps its files.

Installed app (Playout.exe): Documents\\Playout (config, media, playlists, logs) so it
works from Program Files without admin rights. Source checkout: next to the code.
PLAYOUT_HOME overrides both.
"""
import os
import re
import sys

FROZEN = getattr(sys, "frozen", False)


def bundle_dir():
    """Read-only app files (the web page)."""
    if FROZEN:
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def user_home():
    """Writable user files."""
    if os.environ.get("PLAYOUT_HOME"):
        home = os.environ["PLAYOUT_HOME"]
    elif FROZEN:
        home = os.path.join(os.path.expanduser("~"), "Documents", "Playout")
    else:
        home = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(home, exist_ok=True)
    return home


def safe_name(name):
    """A file name as it is (spaces, Sinhala / Tamil letters kept), without folders or characters
    Windows does not allow. Used instead of werkzeug's secure_filename, which turns "My Ad.mp4"
    into "My_Ad.mp4" (file not found) and drops Sinhala letters."""
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    return "" if name in ("", ".", "..") else name

