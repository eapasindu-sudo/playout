#!/bin/bash
# Builds Playout.app on a Mac (Apple Silicon, macOS 13+), the same way the CI macos job does:
#   cd playout && bash build/build-mac.sh
# Needs Python 3.12 (python.org or Homebrew). Result: build/output/Playout-mac.zip and build/dist/Playout.app
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3.12}
command -v "$PY" >/dev/null || PY=python3
"$PY" -m venv build/.venv-mac
source build/.venv-mac/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller
if command -v brew >/dev/null; then              # Sinhala / Tamil text shaping in text CGs
  brew list fribidi >/dev/null 2>&1 || brew install fribidi
  mkdir -p build/dylibs
  cp "$(brew --prefix fribidi)"/lib/libfribidi.0.dylib build/dylibs/ || true
fi
VER=${APP_VERSION:-$(git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || echo 1.0.0)}
echo "VERSION = '$VER'" > version.py
(cd build && APP_VERSION="$VER" pyinstaller --noconfirm playout-mac.spec)
git checkout -- version.py 2>/dev/null || true
mkdir -p build/output
rm -f build/output/Playout-mac.zip
ditto -c -k --keepParent build/dist/Playout.app build/output/Playout-mac.zip
echo "Built Playout $VER: build/dist/Playout.app (copy it to Applications) and build/output/Playout-mac.zip"
