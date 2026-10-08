# PyInstaller spec: builds dist/Playout/Playout.exe (one folder, starts fast)
from PyInstaller.utils.hooks import collect_all

datas, binaries, hidden = [("../web", "web")], [], []
for pkg in ("cyndilib", "av", "imageio_ffmpeg"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h

a = Analysis(["../app.py"], pathex=[".."], binaries=binaries, datas=datas, hiddenimports=hidden)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Playout", console=True, icon=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Playout")
