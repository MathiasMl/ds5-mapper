# PyInstaller spec for Windows build.
# Usage: pyinstaller windows.spec
#
# Produces dist\DS5 Mapper\DS5 Mapper.exe  (onedir layout)
# onedir (not onefile) so the pysdl2-dll DLLs load without extraction overhead.

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_data_files

block_cipher = None

# pysdl2-dll ships SDL2.dll; PyInstaller needs to pick it up.
binaries = collect_dynamic_libs("sdl2dll") + collect_dynamic_libs("sdl2")

datas = [
    ("config.json", "."),
]
# Bundle the icon if present
import os
if os.path.exists("icon.ico"):
    datas.append(("icon.ico", "."))

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=["sdl2", "sdl2.ext", "pynput"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pyobjc", "AppKit", "Quartz", "ApplicationServices", "py2app"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DS5 Mapper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                      # GUI app — no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico" if os.path.exists("icon.ico") else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DS5 Mapper",
)
