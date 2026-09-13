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
    app.setApplicationName('寄存器调试工作台')
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
    saved_size = window.cfg.get('window_size', [1540, 960])
    if not isinstance(saved_size, list) or len(saved_size) != 2 or not all(isinstance(x, int) for x in saved_size):
        saved_size = [1540, 960]
    window.resize(max(1180, min(saved_size[0], available.width())), max(740, min(saved_size[1], available.height())))
    window.show()
    if available.width() <= 1400 or available.height() <= 850:
        window.showMaximized()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
