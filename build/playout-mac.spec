# PyInstaller spec for the Mac (Apple Silicon): builds dist/Playout.app
# No GPU engine (pgpu) or built-in browser (cefcg) on the Mac yet: mixing runs the older way (OpenGL / CPU),
# web CGs open in Google Chrome / Edge, streaming and recording encode with VideoToolbox.
import glob
import os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hidden = [("../web", "web")], [], []
pkgs = ["cyndilib", "websocket", "av", "imageio_ffmpeg", "sounddevice", "_sounddevice_data", "fpdf", "openpyxl",
        "qrcode", "yt_dlp", "certifi", "truststore", "moderngl", "glcontext", "webview"]
for pkg in pkgs:
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d; binaries += b; hidden += h
hidden += ["app", "design", "engine", "streamer", "scheduler", "store", "media", "ndi_io", "paths", "squeeze", "comments", "health",
           "capture", "cg", "system", "audio_out", "psutil", "cgfx", "cgedit", "composer", "recorder",
           "loudness", "gpu", "textlooks", "audiofx", "framequeue", "report", "importer", "updater", "version",
           "textcg", "alerts", "booking", "sdi", "watchfolder", "backup", "splash", "winicon", "certs",
           "ndi_extra", "atem_link", "atemwin", "webcg", "gpucg", "gpudriver", "diag", "moderngl", "glcontext",
           "vmix_link"]
datas += [(os.path.join(SPECPATH, n), ".") for n in ("endora-cgcomposer.ico", "endora-playout.ico")]
binaries += [(f, ".") for f in glob.glob(os.path.join(SPECPATH, "dylibs", "*.dylib"))]   # libfribidi (CI)

a = Analysis(["../desktop.py"], pathex=[".."], binaries=binaries, datas=datas, hiddenimports=hidden)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Playout", console=False, target_arch="arm64")
coll = COLLECT(exe, a.binaries, a.datas, name="Playout")
app = BUNDLE(
    coll,
    name="Playout.app",
    bundle_identifier="com.endora.playout",
    version=os.environ.get("APP_VERSION", "1.0.0"),
    info_plist={
        "CFBundleDisplayName": "Endora Playout",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        # USB capture (ATEM Mini, capture cards) and their sound: macOS asks once
        "NSCameraUsageDescription": "Playout uses USB capture devices (ATEM Mini, capture cards, cameras) as live inputs.",
        "NSMicrophoneUsageDescription": "Playout takes the sound of USB capture devices for live inputs.",
        "NSLocalNetworkUsageDescription": "Playout sends and receives NDI video on the local network.",
        "NSBonjourServices": ["_ndi._tcp"],
    },
)
