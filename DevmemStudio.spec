# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / 'devmem_debug.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'assets/logo.svg'), 'assets'),
           (str(root / 'THIRD_PARTY_NOTICES.md'), '.'),
           (str(root / 'devmem_studio/data/component_catalog.json'), 'devmem_studio/data')],
    hiddenimports=['devmem_studio.smoke'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtMultimedia', 'PySide6.Qt3DCore'],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='DevmemStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(root / 'assets/devmem.ico'),
    version=str(root / 'assets/version_info.txt'),
)
