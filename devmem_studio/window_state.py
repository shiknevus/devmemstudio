"""Top-level dialog styles and reversible owner/child window-state transitions."""
from dataclasses import dataclass
import ctypes
import os
import sys

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog
from shiboken6 import isValid


def _state(widget):
    return widget.windowState() & ~Qt.WindowActive


class ManagedDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._restore_state = Qt.WindowNoState
        # Qt.Dialog already includes the Qt.Window bit, so OR-ing Qt.Window
        # does not change its type. A real Window minimizes to the taskbar
        # rather than leaving Windows' small iconic dialog frame on the desktop.
        # PySide enum inversion truncates high flag bits, including CloseButton.
        # Invert plain integers so every unrelated window hint is retained.
        flags = Qt.WindowType(int(self.windowFlags()) & ~int(Qt.WindowType_Mask)
                              & ~int(Qt.WindowContextHelpButtonHint))
        self.setWindowFlags(flags | Qt.Window | Qt.WindowMinMaxButtonsHint
                            | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint)

    def showEvent(self, event):
        if sys.platform == "win32" and QApplication.platformName() == "windows":
            from .taskbar import bind_relaunch, ensure_taskbar_window
            hwnd = int(self.winId())
            ensure_taskbar_window(hwnd)
            launcher = os.environ.get("DEVMEMSTUDIO_LAUNCHER")
            if launcher and getattr(self, "_taskbar_bound_hwnd", None) != hwnd:
                bind_relaunch(hwnd, launcher)
                self._taskbar_bound_hwnd = hwnd
        super().showEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            current = _state(self)
            if not current & Qt.WindowMinimized:
                self._restore_state = current
            elif not event.oldState() & Qt.WindowMinimized:
                self._restore_state = event.oldState() & ~Qt.WindowActive
        super().changeEvent(event)

    def present(self, activate=True):
        desired = self._restore_state if self.isMinimized() else _state(self)
        parent = self.parentWidget()
        while parent is not None:
            coordinator = getattr(parent, "_window_states", None)
            if coordinator is not None and coordinator.defer_present(self, desired):
                return
            parent = parent.parentWidget()
        previous = self.testAttribute(Qt.WA_ShowWithoutActivating)
        if not activate:
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowState(desired)
        self.show()
        self.setAttribute(Qt.WA_ShowWithoutActivating, previous)
        if activate:
            self.raise_()
            self.activateWindow()


@dataclass
class _DialogState:
    state: object
    geometry: QRect
    visible: bool = False
    snapshot: object = None


class WindowStateCoordinator(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.dialogs = {}
        self.suspended = False
        self.restoring = False
        self._restore_targets = []
        QApplication.instance().installEventFilter(self)

    def _owner_minimized(self):
        if self.owner.isMinimized():
            return True
        # Windows may notify an owned dialog before Qt delivers the owner's
        # WindowStateChange event. Check the native state in that short interval.
        if sys.platform == "win32" and QApplication.platformName() == "windows" and self.owner.windowHandle():
            return bool(ctypes.windll.user32.IsIconic(ctypes.c_void_p(int(self.owner.winId()))))
        return False

    def _belongs(self, widget):
        parent = widget.parentWidget()
        while parent is not None:
            if parent is self.owner:
                return True
            parent = parent.parentWidget()
        return False

    def _native_file_dialog(self, widget):
        # Hiding a native IFileDialog through its Qt proxy closes its selection
        # session. Windows already hides/restores it with its native owner.
        return (isinstance(widget, QFileDialog) and sys.platform == "win32"
                and QApplication.platformName() == "windows"
                and not QApplication.testAttribute(Qt.AA_DontUseNativeDialogs)
                and not widget.testOption(QFileDialog.DontUseNativeDialog))

    def _record(self, widget):
        record = self.dialogs.get(widget)
        if record is None:
            geometry = widget.normalGeometry()
            record = _DialogState(_state(widget), QRect(geometry if geometry.isValid() else widget.geometry()))
            self.dialogs[widget] = record
            widget.destroyed.connect(lambda _object=None, window=widget: self.dialogs.pop(window, None))
        return record

    def _save(self, widget, record, state=None):
        if record.snapshot is None:
            desired = record.state if state is None else state
            restore = getattr(widget, "_restore_state", desired & ~Qt.WindowMinimized)
            record.snapshot = (desired, QRect(record.geometry), restore)

    def _capture_geometry(self, widget):
        if not isValid(widget) or self.suspended or self.restoring or widget.isMinimized():
            return
        record = self.dialogs.get(widget)
        if record is not None:
            geometry = widget.normalGeometry() if widget.isMaximized() else widget.geometry()
            record.geometry = QRect(geometry)

    def _suspend(self):
        self.suspended = True
        for widget in self.owner.findChildren(QDialog):
            if not self._native_file_dialog(widget):
                self._record(widget)
        for widget, record in list(self.dialogs.items()):
            if isValid(widget) and (record.visible or widget.isVisible()):
                self._save(widget, record)
                widget.hide()

    def _suspend_if_needed(self):
        if isValid(self.owner) and (self.suspended or self._owner_minimized()):
            self._suspend()

    def defer_present(self, widget, state):
        if not (self.suspended or self._owner_minimized()):
            return False
        record = self._record(widget)
        record.visible = True
        self._save(widget, record, state)
        # An explicit request to reopen replaces a previously minimized state.
        record.snapshot = (state, QRect(record.geometry), getattr(widget, "_restore_state", state))
        self.suspended = True
        widget.hide()
        return True

    def _resume(self):
        if (not isValid(self.owner) or not self.suspended or self._owner_minimized()
                or not self.owner.isVisible() or getattr(self.owner, "_closing", False)):
            return
        self.restoring = True
        try:
            for widget, record in list(self.dialogs.items()):
                if not isValid(widget) or record.snapshot is None:
                    continue
                state, geometry, restore = record.snapshot
                record.snapshot = None
                previous = widget.testAttribute(Qt.WA_ShowWithoutActivating)
                widget.setAttribute(Qt.WA_ShowWithoutActivating, True)
                if not state & Qt.WindowMinimized:
                    widget.setWindowState(Qt.WindowNoState)
                    if geometry.isValid():
                        widget.setGeometry(geometry)
                widget.setWindowState(state)
                widget.show()
                widget.setAttribute(Qt.WA_ShowWithoutActivating, previous)
                if hasattr(widget, "_restore_state"):
                    widget._restore_state = restore
                record.state, record.geometry, record.visible = state, QRect(geometry), True
                self._restore_targets.append((widget, record, state, QRect(geometry), restore))
        finally:
            self.suspended = False
            # Drain native notifications generated by showing owned windows
            # before accepting new state changes as independent user actions.
            QTimer.singleShot(0, self._finish_restore)

    def _finish_restore(self):
        targets, self._restore_targets = self._restore_targets, []
        try:
            if not isValid(self.owner):
                return
            if self._owner_minimized():
                self.restoring = False
                self._suspend()
                return
            mask = Qt.WindowMinimized | Qt.WindowMaximized | Qt.WindowFullScreen
            for widget, record, state, geometry, restore in targets:
                if not isValid(widget):
                    continue
                if not widget.isVisible():
                    record.visible = False
                    continue
                current = _state(widget)
                matches = bool(current & Qt.WindowMinimized) if state & Qt.WindowMinimized else current & mask == state & mask
                previous = widget.testAttribute(Qt.WA_ShowWithoutActivating)
                widget.setAttribute(Qt.WA_ShowWithoutActivating, True)
                try:
                    if not matches:
                        widget.setWindowState(state)
                    if not state & mask and geometry.isValid() and widget.geometry() != geometry:
                        widget.setGeometry(geometry)
                finally:
                    widget.setAttribute(Qt.WA_ShowWithoutActivating, previous)
                if hasattr(widget, "_restore_state"):
                    widget._restore_state = restore
                record.state = state
        finally:
            self.restoring = False

    def eventFilter(self, watched, event):
        if not isValid(self.owner) or self.restoring:
            return False
        kind = event.type()
        if watched is self.owner:
            if kind == QEvent.WindowStateChange:
                if self._owner_minimized():
                    self._suspend()
                elif self.suspended:
                    QTimer.singleShot(0, self._resume)
            elif kind == QEvent.Hide and getattr(self.owner, "_closing", False):
                self._suspend()
            return False
        if (not isinstance(watched, QDialog) or not isValid(watched) or not self._belongs(watched)
                or self._native_file_dialog(watched)):
            return False
        record = self._record(watched)
        if kind == QEvent.Close:
            record.visible = False
            record.snapshot = None
        elif kind == QEvent.Show:
            record.visible = True
            if self.suspended or self._owner_minimized():
                self._save(watched, record)
                QTimer.singleShot(0, self._suspend_if_needed)
            else:
                record.state = _state(watched)
        elif kind in (QEvent.Hide, QEvent.WindowStateChange):
            if self._owner_minimized():
                self._suspend()
            elif not self.suspended:
                if kind == QEvent.Hide:
                    record.visible = False
                else:
                    record.state = _state(watched)
        if kind in (QEvent.Move, QEvent.Resize, QEvent.Show) and not self.suspended and not watched.isMinimized():
            # A Show/Move filter runs before Qt finishes applying frame offsets.
            QTimer.singleShot(0, lambda window=watched: self._capture_geometry(window))
        return False
