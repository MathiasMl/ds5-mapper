#!/bin/bash
# Build the DS5 Mapper .app bundle.
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "No venv. Run ./setup.sh first." >&2
    exit 1
fi

rm -rf build dist
./venv/bin/python3 setup.py py2app 2>&1 | tail -5

# Ad-hoc sign so the bundle has a stable identity that survives rebuilds;
# macOS TCC permission grants stick to this signature.
codesign --force --deep --sign - "dist/DS5 Mapper.app"

echo ""
echo "Built: dist/DS5 Mapper.app (ad-hoc signed)"
echo "  Test: open 'dist/DS5 Mapper.app'"
echo "  Install: cp -r 'dist/DS5 Mapper.app' /Applications/"
