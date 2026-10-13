# -*- coding: utf-8 -*-
"""Devmem Studio entry point. Run with the project venv or build_exe.bat."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys
import traceback


def main():
    parser = argparse.ArgumentParser(description='Devmem Studio / FPGA workbench')
    parser.add_argument('--smoke-test', metavar='OUTPUT_DIR', help='Run offline acceptance and save a report and screenshots')
    parser.add_argument('--offscreen', action='store_true', help='Render without a visible window')
    args = parser.parse_args()
    if args.offscreen:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QMessageBox
    from devmem_studio.core import user_data_dir
    from devmem_studio import __version__
    from devmem_studio.theme import STYLE
    from devmem_studio.window import MainWindow
    app = QApplication(sys.argv[:1])
    if args.offscreen and sys.platform == 'win32':
        fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
        for filename in ('msyh.ttc', 'msyhbd.ttc', 'consola.ttf', 'consolab.ttf', 'bahnschrift.ttf', 'seguisym.ttf'):
            if (fonts / filename).exists():
                QFontDatabase.addApplicationFont(str(fonts / filename))
    app.setApplicationName('DevmemStudio')
    app.setApplicationVersion(__version__)
    app.setOrganizationName('DevmemStudio')
    app.setStyle('Fusion')
    app.setFont(QFont('Microsoft YaHei UI', 9))
    app.setStyleSheet(STYLE)

    def exception_hook(exc_type, value, tb):
        details = ''.join(traceback.format_exception(exc_type, value, tb))
        try:
            directory = user_data_dir() / 'logs'
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'crash.log').write_text(details, encoding='utf-8')
        except OSError:
            pass
        if args.smoke_test:
            Path(args.smoke_test).mkdir(parents=True, exist_ok=True)
            (Path(args.smoke_test) / 'crash.log').write_text(details, encoding='utf-8')
            app.exit(1)
        else:
            QMessageBox.critical(None, '程序错误', f'{value}\n\n详细信息已写入本机日志目录。')
    sys.excepthook = exception_hook
    if args.smoke_test:
        from devmem_studio.smoke import run_smoke
        return run_smoke(app, Path(args.smoke_test))
    window = MainWindow()
    available = app.primaryScreen().availableGeometry()
    # MainWindow restores the saved normal size and maximized state separately.
    window.show()
    if 'window_maximized' not in window.cfg and (available.width() <= 1400 or available.height() <= 850):
        window.showMaximized()
    launcher = os.environ.get('DEVMEMSTUDIO_LAUNCHER')
    if launcher and sys.platform == 'win32' and not args.offscreen:
        from devmem_studio.taskbar import bind_relaunch
        bind_relaunch(int(window.winId()), launcher)   # pin the single EXE, not its runtime cache
    from PySide6.QtCore import QTimer
    import threading
    from devmem_studio.core import preload_ssh
    # SSH stack warms up once the window is painted; a connect issued earlier just waits on the import.
    QTimer.singleShot(300, lambda: threading.Thread(target=preload_ssh, name='ssh-preload', daemon=True).start())
    QTimer.singleShot(5000, window.check_updates_automatically)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
