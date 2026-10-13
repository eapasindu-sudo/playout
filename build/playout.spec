# PyInstaller spec: builds dist/Playout/Playout.exe (windowed desktop app, one folder)
import sys
from PyInstaller.utils.hooks import collect_all

datas, binaries, hidden = [("../web", "web")], [], []
pkgs = ["cyndilib", "av", "imageio_ffmpeg", "sounddevice", "_sounddevice_data", "fpdf", "openpyxl", "qrcode", "yt_dlp"]
if sys.platform == "win32":
    pkgs += ["webview", "clr_loader", "pythonnet"]
for pkg in pkgs:
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d; binaries += b; hidden += h
hidden += ["app", "engine", "streamer", "scheduler", "store", "media", "ndi_io", "paths", "squeeze", "comments",
           "capture", "cg", "system", "audio_out", "psutil", "cgfx", "cgedit", "composer", "recorder",
           "loudness", "report", "importer", "updater", "version",
           "textcg", "alerts", "booking", "sdi", "watchfolder", "backup", "splash", "winicon"]

import glob, os
datas += [(os.path.join(SPECPATH, n), ".") for n in ("endora-cgcomposer.ico", "endora-playout.ico")]   # window icons
binaries += [(f, ".") for f in glob.glob(os.path.join(SPECPATH, "dlls", "*.dll"))]   # libfribidi-0.dll (CI)

a = Analysis(["../desktop.py"], pathex=[".."], binaries=binaries, datas=datas, hiddenimports=hidden)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Playout",
          console=False, icon="playout.ico")
coll = COLLECT(exe, a.binaries, a.datas, name="Playout")
