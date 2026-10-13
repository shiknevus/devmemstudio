"""Exercise real Windows window states outside the visible desktop; test helper only."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QMainWindow, QVBoxLayout
from devmem_studio.window_state import ManagedDialog, WindowStateCoordinator


def main():
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    owner = QMainWindow()
    owner_surface = QWidget()
    owner.setCentralWidget(owner_surface)
    owner.resize(300, 200)
    owner.move(-32000, -32000)
    owner._window_states = states = WindowStateCoordinator(owner)
    child = ManagedDialog(owner)
    child_surface = QWidget()
    layout = QVBoxLayout(child)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(child_surface)
    child.resize(200, 120)
    child.move(-32000, -32000)
    user32 = ctypes.WinDLL("user32")
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    checks = []

    # Maximized probes stay invisible and do not take keyboard or mouse input.
    for window in (owner, child):
        window.setAttribute(Qt.WA_ShowWithoutActivating, True)
        hwnd = wintypes.HWND(int(window.winId()))
        style = user32.GetWindowLongPtrW(hwnd, -20)
        user32.SetWindowLongPtrW(hwnd, -20, style | 0x00080000 | 0x00000020 | 0x08000000)
        user32.SetLayeredWindowAttributes(hwnd, 0, 0, 2)
    owner.show()
    child.present(activate=False)

    def check(value, name):
        if not value:
            raise AssertionError(name)
        checks.append(name)

    def settle():
        QTest.qWait(250)
        app.processEvents()

    def native_restore(window):
        user32.SendMessageW(wintypes.HWND(int(window.winId())), 0x0112, 0xF120, 0)
        settle()

    def full_content(window, surface, name):
        rect = wintypes.RECT()
        user32.GetClientRect(wintypes.HWND(int(window.winId())), ctypes.byref(rect))
        ratio = window.devicePixelRatioF()
        check(abs(window.width() * ratio - rect.right) <= 1 and abs(window.height() * ratio - rect.bottom) <= 1,
              name + ": Qt size matches native client")
        check(surface.width() == window.contentsRect().width() and surface.height() == window.contentsRect().height(),
              name + ": content fills restored window")

    try:
        settle()
        for cycle in range(3):
            child.showMinimized()
            settle()
            QApplication.sendEvent(owner, QEvent(QEvent.WindowActivate))
            settle()
            check(child.isMinimized(), f"cycle {cycle}: owner activation leaves child minimized")
            check(states._taskbar_key and states._taskbar_key[0] == int(child.winId()),
                  f"cycle {cycle}: taskbar represents minimized child")
            native_restore(child)
        owner.showMaximized()
        child.showMaximized()
        settle()
        full_content(owner, owner_surface, "initial maximized owner")
        full_content(child, child_surface, "initial maximized child")
        for cycle in range(3):
            child.showMinimized()
            settle()
            owner.showMinimized()
            settle()
            native_restore(owner)
            native_restore(child)
            check(owner.isMaximized() and child.isMaximized(), f"maximized cycle {cycle}: states preserved")
            full_content(owner, owner_surface, f"maximized cycle {cycle}: owner")
            full_content(child, child_surface, f"maximized cycle {cycle}: child")
            check(not child.isMinimized() and child.isVisible(), f"cycle {cycle}: native child restore")
            child.showMinimized()
            settle()
            owner.showMinimized()
            settle()
            check(owner.isMinimized() and not child.isVisible(), f"cycle {cycle}: owner minimize")
            check(states._taskbar_key and states._taskbar_key[0] == int(owner.winId()),
                  f"cycle {cycle}: taskbar represents minimized owner")
            native_restore(owner)
            check(not owner.isMinimized() and child.isMinimized(), f"cycle {cycle}: native owner restore preserves child state")
            native_restore(child)
        child.showMinimized()
        settle()
        child.close()
        settle()
        check(not child.isVisible(), "closed child stays closed")
        check(states._taskbar_key and states._taskbar_key[0] == int(owner.winId()), "taskbar returns to owner on close")
        print(json.dumps({"platform": app.platformName(), "checks": checks}))
    finally:
        child.close()
        owner.close()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
