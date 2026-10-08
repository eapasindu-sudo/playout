"""Where Playout keeps its files.

Installed app (Playout.exe): Documents\\Playout (config, media, playlists, logs) so it
works from Program Files without admin rights. Source checkout: next to the code.
PLAYOUT_HOME overrides both.
"""
import os
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
