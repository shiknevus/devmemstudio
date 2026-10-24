# -*- coding: utf-8 -*-
"""Devmem Studio entry point. Run with the project venv or build_exe.bat."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys
import threading
import traceback


def main():
    parser = argparse.ArgumentParser(description='Devmem Studio / FPGA workbench')
    parser.add_argument('--smoke-test', metavar='OUTPUT_DIR', help='Run offline acceptance and save a report and screenshots')
    parser.add_argument('--offscreen', action='store_true', help='Render without a visible window')
    args = parser.parse_args()
    from devmem_studio import launcher_splash
    launcher_splash.stage(launcher_splash.QT)   # the single-file launcher's bar follows these stages
    if args.offscreen:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QMessageBox
    launcher_splash.stage(launcher_splash.MODULES)
    from devmem_studio.core import user_data_dir
    from devmem_studio import __version__
    from devmem_studio.theme import STYLE
    from devmem_studio.window import MainWindow
    launcher_splash.stage(launcher_splash.UI)
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

    showing = []   # one dialog at a time: a failing paint/timer slot would otherwise stack them

    def record_crash(details, name='crash.log'):
        try:
            directory = user_data_dir() / 'logs'
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(details, encoding='utf-8')
        except OSError:
            pass

    def exception_hook(exc_type, value, tb):
        details = ''.join(traceback.format_exception(exc_type, value, tb))
        record_crash(details)
        if args.smoke_test:
            Path(args.smoke_test).mkdir(parents=True, exist_ok=True)
            (Path(args.smoke_test) / 'crash.log').write_text(details, encoding='utf-8')
            app.exit(1)
        elif not showing:
            showing.append(True)
            try:
                QMessageBox.critical(None, '程序错误', f'{value}\n\n详细信息已写入本机日志目录。')
            finally:
                showing.clear()

    def thread_hook(hook):
        if hook.exc_type is not SystemExit:   # background threads: log only, no dialog off the GUI thread
            record_crash(f'[thread {hook.thread.name if hook.thread else "?"}]\n'
                         + ''.join(traceback.format_exception(hook.exc_type, hook.exc_value, hook.exc_traceback)),
                         'thread-crash.log')
    sys.excepthook = exception_hook
    threading.excepthook = thread_hook
    if args.smoke_test:
        from devmem_studio.smoke import run_smoke
        return run_smoke(app, Path(args.smoke_test))
    launcher_splash.stage(launcher_splash.WINDOW)
    window = MainWindow()
    available = app.primaryScreen().availableGeometry()
    # MainWindow restores the saved normal size and maximized state separately.
    if 'window_maximized' not in window.cfg and (available.width() <= 1400 or available.height() <= 850):
        # Before show(): the window opens maximized instead of animating from normal size.
        window.setWindowState(window.windowState() | Qt.WindowMaximized)
    if sys.platform == 'win32' and not args.offscreen:
        launcher_splash.show_main_window(window)   # the splash's bar fills and it closes first
    else:
        window.show()
    launcher = os.environ.get('DEVMEMSTUDIO_LAUNCHER')
    if launcher and sys.platform == 'win32' and not args.offscreen:
        from devmem_studio.taskbar import bind_relaunch
        bind_relaunch(int(window.winId()), launcher)   # pin the single EXE, not its runtime cache
    from PySide6.QtCore import QTimer
    if os.environ.get('DEVMEMSTUDIO_UPDATE_READY'):   # started by the update helper: confirm once the window is painted
        from devmem_studio.updater import report_started
        QTimer.singleShot(0, report_started)
    from devmem_studio.core import preload_ssh
    # SSH stack warms up once the window is painted; a connect issued earlier just waits on the import.
    QTimer.singleShot(300, lambda: threading.Thread(target=preload_ssh, name='ssh-preload', daemon=True).start())
    QTimer.singleShot(5000, window.check_updates_automatically)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
