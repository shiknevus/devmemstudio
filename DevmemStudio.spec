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
    # GitHub updates use urllib + ssl: retain _ssl.pyd and both OpenSSL DLLs.
    'ucrtbase.dll',              # 1.1 MB —— Win10+ 始终使用系统 UCRT，随包副本不会被加载
    '_hashlib.pyd',              # 0.07 MB —— 见 excludes '_hashlib'（无 pbkdf2_hmac/scrypt 使用）
    '/qminimal.dll',             # 0.06 MB 平台插件 —— 仅用 qwindows/qoffscreen
)

# 单文件启动器对整个运行库做 LZMS 压缩；PYZ/PKG 先存储不压缩，避免 zlib 后再压不动
from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter
ZlibArchiveWriter._COMPRESSION_LEVEL = 0
CArchiveWriter._COMPRESSION_LEVEL = 0


a = Analysis(
    [str(root / 'devmem_debug.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'assets/logo.svg'), 'assets'),
           (str(root / 'assets/updater/apply_update.ps1'), 'assets/updater'),
           (str(root / 'assets/bitpack/pack_bit.exe'), 'assets/bitpack'),
           (str(root / 'assets/bitpack/使用说明.txt'), 'assets/bitpack'),
           (str(root / 'assets/regmon/regmon-aarch64'), 'assets/regmon'),
           (str(root / 'THIRD_PARTY_NOTICES.md'), '.'),
           (str(root / 'devmem_studio/data/component_catalog.json'), 'devmem_studio/data')],
    hiddenimports=['devmem_studio.smoke', 'devmem_studio.taskbar'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtMultimedia', 'PySide6.Qt3DCore',
              'PySide6.QtPdf', 'PySide6.QtVirtualKeyboard', 'PySide6.QtNetwork',
              'PySide6.QtOpenGL', 'PySide6.QtWebChannel', 'PySide6.QtWebSockets',
              'invoke',  # paramiko 仅在 ssh_config "Match exec" 时可选导入；本程序不解析 ssh_config
              '_hashlib',  # OpenSSL 版 hashlib；内置 _sha2/_md5/_sha1/_sha3/_blake2 覆盖全部所用算法
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

# ---- 精简 datas：程序不安装 QTranslator，Qt 翻译全部不打包 ----
kept_datas = []
for dest, src, typecode in a.datas:
    srcL = src.replace('\\', '/').lower()
    if 'pyside6' in srcL and '/translations/' in srcL:
        dropped.append(('translation', src.split('\\')[-1]))
        continue
    kept_datas.append((dest, src, typecode))
a.datas = kept_datas

if dropped:
    print(f'[slim] dropped {len(dropped)} binaries/data:')
    for d in dropped:
        print(f'  - {d[0]}  {d[1]}')

# Runtime staging directory; tools/build_singlefile.py embeds it into the single distributed EXE.
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='DevmemStudio', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False,
    icon=str(root / 'assets/devmem.ico'), version=str(root / 'assets/version_info.txt'),
)
bundle = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='_runtime')
