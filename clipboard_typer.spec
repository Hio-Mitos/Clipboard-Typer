# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec for Clipboard Typer.
#
# Produces a single windowed ClipboardTyper.exe (no console window) with the
# app icon baked in, ready to be staged into packaging/ for MSIX packaging
# (see packaging/build_msix.ps1) or handed out directly as a standalone EXE.
#
# Build with:
#     pyinstaller clipboard_typer.spec

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# winsdk (the Windows Runtime / WinRT Python bindings, used for the
# "Run at startup" feature's StartupTask API on the packaged/MSIX build)
# ships its own native extension modules and WinMD metadata that
# PyInstaller's static import scanner can't see on its own - collect_all()
# pulls in everything the package needs (binaries, data files, and every
# submodule it might import) rather than hand-listing them one by one.
winsdk_datas, winsdk_binaries, winsdk_hiddenimports = collect_all('winsdk')

a = Analysis(
    ['clipboard_typer.py'],
    pathex=[],
    binaries=winsdk_binaries,
    datas=winsdk_datas,
    # pywin32 modules are sometimes missed by PyInstaller's automatic
    # dependency scan - list them explicitly so the frozen EXE doesn't fail
    # at runtime with "DLL load failed" / ImportError.
    hiddenimports=[
        'win32timezone',
        'win32gui',
        'win32con',
        'win32process',
        'win32api',
        'win32event',
        'winerror',
    ] + winsdk_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ClipboardTyper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no console window - same as pythonw.exe
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='packaging/app_icon.ico',
)
