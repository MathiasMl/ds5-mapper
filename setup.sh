#!/bin/bash
# One-command dev setup. Creates venv + installs deps.
# Requires: macOS with Homebrew + Python 3.11+.
set -e
cd "$(dirname "$0")"

echo "==> Checking prerequisites"
command -v python3 >/dev/null || { echo "Python 3 not found. Install from python.org or Homebrew."; exit 1; }
command -v brew >/dev/null || { echo "Homebrew not found. Install from https://brew.sh"; exit 1; }

if ! brew list hidapi >/dev/null 2>&1; then
    echo "==> Installing hidapi via Homebrew"
    brew install hidapi
fi

echo "==> Creating venv"
python3 -m venv venv
source venv/bin/activate

echo "==> Installing Python deps"
pip install --quiet --upgrade pip
pip install --quiet pysdl2 pysdl2-dll pynput pyobjc-framework-Quartz pyobjc-framework-Cocoa

echo ""
echo "==> Done."
echo "   Run the app from source:  ./venv/bin/python3 app.py"
echo "   Build .app bundle:        ./build.sh"
