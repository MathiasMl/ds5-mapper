"""
py2app build for the DS5 Mapper.

Build:
    ./venv/bin/python3 setup.py py2app

Result: dist/DS5 Mapper.app
Open it with: open "dist/DS5 Mapper.app"
Or drag it to /Applications for a permanent install.
"""
from setuptools import setup

APP = ["app.py"]
# Config files we want available alongside the app. On first launch, if the
# user's ~/.config/ds5-league/config.json is missing, the app copies these in.
DATA_FILES = [
    ("", ["config.json", "mapper.py"]),
]
OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "DS5 Mapper",
        "CFBundleDisplayName": "DS5 Mapper",
        "CFBundleIdentifier": "com.mathias.ds5-mapper",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "NSHumanReadableCopyright": "MIT",
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": False,
        "NSAppleEventsUsageDescription":
            "DS5 Mapper sends keyboard and mouse events to translate controller input.",
    },
    "packages": ["sdl2", "pynput", "tkinter"],
    "includes": ["AppKit", "Quartz"],
    "excludes": ["pytest", "numpy", "scipy", "PIL", "pandas"],
    "iconfile": "icon.icns",
}

setup(
    app=APP,
    name="DS5 Mapper",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
