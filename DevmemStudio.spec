# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)

DROP_BIN_SUBSTR = (
    'opengl32sw.dll',            # 20.6 MB software OpenGL —— 无 QOpenGL 依赖，QPainter 走 raster
    '/qt6qml',                   # QML 相关 DLL (Qt6Qml/QmlMeta/QmlModels/QmlWorkerScript)
    '/qt6quick.dll',             # Quick —— 未使用
    '/qt6pdf.dll',               # PDF —— 未使用
    '/qt6virtualkeyboard.dll',   # 虚拟键盘 —— 未使用
    'securecrt',                 # 误入的第三方程序 OpenSSL (libcrypto/libssl x64)，~7.3 MB
    '/qt6network.dll',           # 1.75 MB —— 无 QtNetwork 引用，唯一 importer 为下述 touch 插件
    '/qt6opengl.dll',            # 1.99 MB —— 无 QOpenGL 引用，QPainter 走 raster
    '/qdirect2d.dll',            # 1.07 MB D2D 平台插件 —— 仅用 qwindows/qoffscreen
    '/qtuiotouchplugin.dll',     # 0.10 MB 触摸插件 —— 未使用，且是 Qt6Network 唯一 importer
    '/qtvirtualkeyboardplugin.dll',  # 0.03 MB 虚拟键盘输入上下文 —— Qt6VirtualKeyboard 已排除
    '/imageformats/qjpeg.dll',   # 0.59 MB —— 无 JPEG 加载
    '/imageformats/qwebp.dll',   # 0.56 MB —— 无 WebP 加载
    '/imageformats/qtiff.dll',   # 0.45 MB —— 无 TIFF 加载
    '/imageformats/qgif.dll',    # 0.05 MB —— 无 GIF 加载
    '/imageformats/qicns.dll',   # 0.06 MB —— 无 ICNS 加载
    '/imageformats/qico.dll',    # 0.05 MB —— 图标用 logo.svg，无 .ico 运行时加载
    '/imageformats/qtga.dll',    # 0.04 MB —— 无 TGA 加载
    '/imageformats/qwbmp.dll',   # 0.04 MB —— 无 WBMP 加载
    '/imageformats/qpdf.dll',    # 0.04 MB —— 无 PDF 图像加载
    'libssl-3.dll',              # 0.79 MB OpenSSL TLS —— SSH 用 cryptography，无需 ssl 模块
    '_ssl.pyd',                  # 0.18 MB —— 无代码 import ssl（见 excludes 'ssl'）
)
# 仅保留 zh_CN 与 en 的 Qt 翻译，其余 ~100 个 qm 不打包
KEEP_TRANSLATION = ('qt_zh_CN.qm', 'qtbase_zh_CN.qm', 'qt_en.qm', 'qtbase_en.qm')
KEEP_TRANSLATION_BASE = ('qtbase_zh_CN.qm', 'qtbase_en.qm')


def _keep_translation(name: str) -> bool:
    n = name.rstrip('/').rsplit('/', 1)[-1].lower()
    return any(k.lower() == n for k in KEEP_TRANSLATION)


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
              'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtMultimedia', 'PySide6.Qt3DCore',
              'PySide6.QtPdf', 'PySide6.QtVirtualKeyboard', 'PySide6.QtNetwork',
              'PySide6.QtOpenGL', 'PySide6.QtWebChannel', 'PySide6.QtWebSockets',
              'ssl',  # SSH 走 paramiko(用 cryptography)，无 TLS；排除后一并剔除 _ssl.pyd / libssl-3.dll
              'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning',
              'PySide6.QtSerialPort', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtCharts',
              'PySide6.QtDataVisualization', 'PySide6.QtHelp', 'PySide6.QtDesigner',
              'PySide6.QtPdfWidgets', 'PySide6.QtQuickWidgets', 'PySide6.QtWebEngineQuick'],
    noarchive=False,
    optimize=1,
)

# ---- 精简 binaries：剔除确定无用的 Qt 大件 ----
kept = []
dropped = []
for dest, src, typecode in a.binaries:
    srcL = src.replace('\\', '/').lower()
    destL = dest.replace('\\', '/').lower()
    if any(s in destL or s.lower() in srcL for s in DROP_BIN_SUBSTR):
        dropped.append((dest, src.split('\\')[-1]))
        continue
    kept.append((dest, src, typecode))
a.binaries = kept

# ---- 精简 datas：translations 只留 zh_CN/en ----
kept_datas = []
for dest, src, typecode in a.datas:
    srcL = src.replace('\\', '/').lower()
    if 'pyside6' in srcL and '/translations/' in srcL:
        if not _keep_translation(srcL):
            dropped.append(('translation', src.split('\\')[-1]))
            continue
    kept_datas.append((dest, src, typecode))
a.datas = kept_datas

if dropped:
    print(f'[slim] dropped {len(dropped)} binaries/data:')
    for d in dropped:
        print(f'  - {d[0]}  {d[1]}')

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