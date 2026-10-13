"""Top-level dialog styles and reversible owner/child window-state transitions."""
from dataclasses import dataclass
import ctypes
import os
import sys

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QWidget
from shiboken6 import isValid


def _state(widget):
    return widget.windowState() & ~Qt.WindowActive


def _is_normal(widget):
    if _state(widget) & (Qt.WindowMinimized | Qt.WindowMaximized | Qt.WindowFullScreen):
        return False
    if sys.platform == "win32" and QApplication.platformName() == "windows" and widget.windowHandle():
        hwnd = ctypes.c_void_p(int(widget.winId()))
        return not (ctypes.windll.user32.IsZoomed(hwnd) or ctypes.windll.user32.IsIconic(hwnd))
    return True


def _saved_size(value):
    return (isinstance(value, list) and len(value) == 2
            and all(type(size) is int and 64 <= size <= 16384 for size in value))


def _refresh_restored_window(widget):
    """Synchronize restored client size and invalidate the complete backing store."""
    if not isValid(widget) or not widget.isVisible() or widget.isMinimized():
        return
    if sys.platform == "win32" and QApplication.platformName() == "windows" and widget.windowHandle():
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        hwnd = wintypes.HWND(int(widget.winId()))
        rect = wintypes.RECT()
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        if user32.GetClientRect(hwnd, ctypes.byref(rect)):
            ratio = widget.devicePixelRatioF()
            if abs(widget.width() * ratio - rect.right) > 1 or abs(widget.height() * ratio - rect.bottom) > 1:
                # Qt can retain the pre-minimize client size while Windows has
                # already restored a maximized HWND. Deliver the native size.
                user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                user32.SendMessageW.restype = wintypes.LPARAM
                user32.SendMessageW(hwnd, 0x0005, 2 if widget.isMaximized() else 0,
                                    (rect.bottom << 16) | (rect.right & 0xFFFF))
    if widget.layout() is not None:
        widget.layout().invalidate()
        widget.layout().activate()
    widget.update()
    for child in widget.findChildren(QWidget):
        if child.isVisible() and child.window() is widget:
            child.update()
    if widget.windowHandle() is not None:
        widget.windowHandle().requestUpdate()


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
        parent = self.parentWidget()
        while parent is not None:
            coordinator = getattr(parent, "_window_states", None)
            if coordinator is not None:
                coordinator.prepare_dialog(self)
                break
            parent = parent.parentWidget()
        super().showEvent(event)
        self._detach_from_owner()

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            current = _state(self)
            if not current & Qt.WindowMinimized:
                self._restore_state = current
            elif not event.oldState() & Qt.WindowMinimized:
                self._restore_state = event.oldState() & ~Qt.WindowActive
            if event.oldState() & Qt.WindowMinimized and not current & Qt.WindowMinimized:
                QTimer.singleShot(0, lambda: _refresh_restored_window(self))
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            # Maximizing can reapply the owner link after showEvent.
            QTimer.singleShot(0, self._detach_from_owner)

    def _detach_from_owner(self):
        if not isValid(self) or sys.platform != "win32" or QApplication.platformName() != "windows" or not self.windowHandle():
            return
        from .taskbar import ensure_taskbar_window, release_window_owner, bind_relaunch
        hwnd = int(self.winId())
        ensure_taskbar_window(hwnd)
        release_window_owner(hwnd)
        launcher = os.environ.get("DEVMEMSTUDIO_LAUNCHER")
        if launcher and getattr(self, "_taskbar_bound_hwnd", None) != hwnd:
            bind_relaunch(hwnd, launcher)
            self._taskbar_bound_hwnd = hwnd
        parent = self.parentWidget()
        while parent is not None:
            coordinator = getattr(parent, "_window_states", None)
            if coordinator is not None:
                coordinator._queue_taskbar(force=True)
                break
            parent = parent.parentWidget()

    def nativeEvent(self, event_type, message):
        if sys.platform == "win32" and bytes(event_type) == b"windows_generic_MSG":
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            # Explorer re-creates tabs after restarting. Reapply the single-tab
            # policy when Windows announces that this window's tab was created.
            created = getattr(self, "_taskbar_created_message", None)
            if created is None:
                register = ctypes.windll.user32.RegisterWindowMessageW
                register.argtypes = [wintypes.LPCWSTR]
                register.restype = wintypes.UINT
                created = self._taskbar_created_message = register("TaskbarButtonCreated")
            if msg.message == created:
                QTimer.singleShot(0, self._detach_from_owner)
        return super().nativeEvent(event_type, message)

    def present(self, activate=True):
        coordinator = None
        parent = self.parentWidget()
        while parent is not None:
            coordinator = getattr(parent, "_window_states", None)
            if coordinator is not None:
                coordinator.prepare_dialog(self)
                break
            parent = parent.parentWidget()
        desired = self._restore_state if self.isMinimized() else _state(self)
        if coordinator is not None and coordinator.defer_present(self, desired):
            return
        previous = self.testAttribute(Qt.WA_ShowWithoutActivating)
        if not activate:
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        if coordinator is not None and (not self.isVisible() or self.isMinimized()):
            geometry = coordinator.normal_geometry(self)
            self.setWindowState(Qt.WindowNoState)
            if geometry.isValid():
                self.setGeometry(geometry)
            self.show()
            self.setWindowState(desired)
        else:
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
    closed: bool = False
    closing: bool = False
    prepared: bool = False


class WindowStateCoordinator(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.dialogs = {}
        self.suspended = False
        self.restoring = False
        self._revealing = False
        self._restore_targets = []
        self._owner_geometry = QRect(owner.geometry())
        self._normal_restore_pending = {}
        self.last_minimized = None
        self._minimized_order = []
        self._active_window = owner
        self._taskbar_key = None
        self._taskbar_timer = QTimer(self)
        self._taskbar_timer.setSingleShot(True)
        self._taskbar_timer.timeout.connect(self._sync_taskbar)
        QApplication.instance().installEventFilter(self)

    def prepare_dialog(self, widget):
        record = self._record(widget)
        if record.prepared:
            return
        record.prepared = True
        config = getattr(self.owner, "cfg", {})
        settings = config.get("dialog_windows", {}) if isinstance(config, dict) else {}
        saved = settings.get(type(widget).__name__, {}) if isinstance(settings, dict) else {}
        if not isinstance(saved, dict) or not _saved_size(saved.get("size")):
            if not _is_normal(widget):
                geometry = widget.normalGeometry()
                if geometry.isValid():
                    record.geometry = QRect(geometry)
            else:
                self._capture_geometry(widget)
            return
        desired = _state(widget)
        widget.setWindowState(Qt.WindowNoState)
        widget.resize(*saved["size"])
        record.geometry = QRect(widget.geometry())
        if saved.get("maximized") is True:
            desired = Qt.WindowMaximized
        widget.setWindowState(desired)

    def normal_geometry(self, widget):
        if widget is self.owner:
            self._capture_owner_geometry()
            return QRect(self._owner_geometry)
        self._capture_geometry(widget)
        return QRect(self._record(widget).geometry)

    def save_window_preferences(self):
        config = getattr(self.owner, "cfg", None)
        if not isinstance(config, dict):
            return
        size = self.normal_geometry(self.owner).size()
        config["window_size"] = [size.width(), size.height()]
        config["window_maximized"] = bool(_state(self.owner) & Qt.WindowMaximized)
        raw = config.get("dialog_windows", {})
        saved = dict(raw) if isinstance(raw, dict) else {}
        for widget, record in list(self.dialogs.items()):
            if not isValid(widget) or not isinstance(widget, ManagedDialog) or not record.prepared:
                continue
            geometry = self.normal_geometry(widget)
            state = record.snapshot[0] if record.snapshot is not None else _state(widget)
            saved[type(widget).__name__] = {"size": [geometry.width(), geometry.height()],
                                          "maximized": bool(state & Qt.WindowMaximized)}
        config["dialog_windows"] = saved

    def _capture_owner_geometry(self):
        if (isValid(self.owner) and not self.restoring and self.owner not in self._normal_restore_pending
                and _is_normal(self.owner)):
            self._owner_geometry = QRect(self.owner.geometry())

    def _restore_normal_size(self, widget):
        geometry = self._owner_geometry if widget is self.owner else self._record(widget).geometry
        expected = geometry.size()
        self._normal_restore_pending[widget] = expected
        QTimer.singleShot(0, lambda: self._finish_normal_restore(widget, expected))

    def _finish_normal_restore(self, widget, expected):
        try:
            if not isValid(widget) or not widget.isVisible() or not _is_normal(widget):
                return
            if widget is self.owner:
                # Maximized responsive layouts can raise the size floor. Relax
                # it before restoring the earlier compact window size.
                widget.setMinimumSize(min(widget.minimumWidth(), expected.width()),
                                      min(widget.minimumHeight(), expected.height()))
            if widget.size() != expected:
                widget.resize(expected)
            _refresh_restored_window(widget)
        finally:
            self._normal_restore_pending.pop(widget, None)
            if isValid(widget):
                if widget is self.owner:
                    self._capture_owner_geometry()
                else:
                    self._capture_geometry(widget)

    def _owner_minimized(self):
        if self.owner.isMinimized():
            return True
        # Windows may notify an owned dialog before Qt delivers the owner's
        # WindowStateChange event. Check the native state in that short interval.
        if sys.platform == "win32" and QApplication.platformName() == "windows" and self.owner.windowHandle():
            return bool(ctypes.windll.user32.IsIconic(ctypes.c_void_p(int(self.owner.winId()))))
        return False

    def restore_last_minimized(self):
        target = self.last_minimized
        if (target is None or not isValid(target) or getattr(self.owner, "_closing", False)
                or self.restoring):
            return False
        self._forget_minimized(target)
        if self.owner.isMinimized():
            self.owner.setWindowState(_state(self.owner) & ~Qt.WindowMinimized)
            self.owner.show()
        if self.suspended:
            self._resume()
            # Wait for geometry/state restoration before presenting the target.
            QTimer.singleShot(0, lambda: self._present_target(target))
        else:
            self._present_target(target)
        return True

    def _present_target(self, target):
        if not isValid(target) or getattr(self.owner, "_closing", False):
            return
        record = self.dialogs.get(target)
        if target is not self.owner and record is not None and (record.closed or record.closing):
            return
        if isinstance(target, ManagedDialog):
            target.present()
        else:
            target.show()
            target.raise_()
            target.activateWindow()

    def _note_minimized(self, widget):
        self._minimized_order = [window for window in self._minimized_order if window is not widget and isValid(window)]
        self._minimized_order.append(widget)
        self.last_minimized = widget
        self._queue_taskbar()

    def _forget_minimized(self, widget):
        self._minimized_order = [window for window in self._minimized_order if window is not widget and isValid(window)]
        self.last_minimized = self._minimized_order[-1] if self._minimized_order else None
        self._queue_taskbar()

    def _taskbar_target(self):
        for window in reversed(self._minimized_order):
            record = self.dialogs.get(window)
            if isValid(window) and window.isMinimized() and not (record and (record.closed or record.closing)):
                return window
        window = self._active_window
        record = self.dialogs.get(window)
        if isValid(window) and window.isVisible() and not (record and (record.closed or record.closing)):
            return window
        return self.owner

    def _queue_taskbar(self, force=False):
        if not isValid(self) or not isValid(self._taskbar_timer):
            return
        if force:
            self._taskbar_key = None
        if not self._taskbar_timer.isActive():
            self._taskbar_timer.start(0)

    def _sync_taskbar(self):
        if (not isValid(self.owner) or getattr(self.owner, "_closing", False)
                or self.restoring
                or sys.platform != "win32" or QApplication.platformName() != "windows"
                or not self.owner.windowHandle()):
            return
        windows = [self.owner] + [window for window in self.dialogs
                                  if isValid(window) and isinstance(window, ManagedDialog) and window.windowHandle()]
        target = self._taskbar_target()
        if target not in windows:
            target = self.owner
        hwnd = int(target.winId())
        others = tuple(int(window.winId()) for window in windows if window is not target)
        key = hwnd, others
        if key != self._taskbar_key:
            from .taskbar import select_taskbar_tab
            if select_taskbar_tab(hwnd, others):
                self._taskbar_key = key

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
            widget.destroyed.connect(lambda _object=None, window=widget: self._dialog_destroyed(window))
        return record

    def _dialog_destroyed(self, widget):
        self.dialogs.pop(widget, None)
        self._normal_restore_pending.pop(widget, None)
        self._forget_minimized(widget)
        if self._active_window is widget:
            self._active_window = self.owner

    def _finish_close(self, widget):
        if not isValid(widget):
            return
        record = self.dialogs.get(widget)
        if record is None:
            return
        record.closing = False
        # A busy dialog may ignore Close while cancelling its worker. Keep its
        # geometry, minimized history and snapshot until it really disappears.
        if not widget.isVisible():
            record.visible = False
            record.snapshot = None
            record.closed = True
            self._forget_minimized(widget)
            schedule_save = getattr(self.owner, "_schedule_save", None)
            if schedule_save is not None:
                schedule_save()
        self._queue_taskbar()

    def _save(self, widget, record, state=None):
        if record.snapshot is None:
            desired = record.state if state is None else state
            restore = getattr(widget, "_restore_state", desired & ~Qt.WindowMinimized)
            record.snapshot = (desired, QRect(record.geometry), restore)

    def _capture_geometry(self, widget):
        if (not isValid(widget) or self.suspended or self.restoring or widget in self._normal_restore_pending
                or not _is_normal(widget)):
            return
        record = self.dialogs.get(widget)
        if record is not None:
            record.geometry = QRect(widget.geometry())

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

    def _reveal_owner(self):
        if (self._revealing or not isValid(self.owner) or self.restoring or self.suspended
                or self.owner.isMinimized() or not self.owner.isVisible()):
            return
        if sys.platform != "win32" or QApplication.platformName() != "windows":
            return
        covered = [widget for widget in list(self.dialogs)
                   if isValid(widget) and widget.isVisible() and widget.isMaximized()]
        if not covered:
            return
        self._revealing = True
        try:
            from .taskbar import release_window_owner
            for widget in covered:
                detach = getattr(widget, "_detach_from_owner", None)
                if detach is not None:
                    detach()
                elif widget.windowHandle():
                    release_window_owner(int(widget.winId()))
            hwnd = int(self.owner.winId())
            ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
            self.owner.raise_()
        finally:
            self._revealing = False

    def _resume(self):
        if (not isValid(self.owner) or not self.suspended or self._owner_minimized()
                or not self.owner.isVisible() or getattr(self.owner, "_closing", False)):
            return
        self.restoring = True
        try:
            for widget, record in list(self.dialogs.items()):
                if not isValid(widget) or record.snapshot is None or record.closed or record.closing:
                    continue
                state, geometry, restore = record.snapshot
                record.snapshot = None
                previous = widget.testAttribute(Qt.WA_ShowWithoutActivating)
                widget.setAttribute(Qt.WA_ShowWithoutActivating, True)
                if not state & Qt.WindowMinimized:
                    widget.setWindowState(Qt.WindowNoState)
                    if geometry.isValid():
                        widget.setGeometry(geometry)
                if state & Qt.WindowMinimized:
                    widget.setWindowState(state)
                    widget.show()
                else:
                    # Maximize after showing the normal client, so Windows and
                    # Qt deliver the full-size layout/resize in the same order.
                    widget.show()
                    widget.setWindowState(state)
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
                if record.closed or record.closing or not widget.isVisible():
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
                if not state & Qt.WindowMinimized:
                    QTimer.singleShot(0, lambda window=widget: _refresh_restored_window(window))
        finally:
            self.restoring = False
            self._queue_taskbar()

    def eventFilter(self, watched, event):
        if not isValid(self.owner):
            return False
        kind = event.type()
        if self.restoring and kind != QEvent.Close:
            return False
        if watched is self.owner:
            if (kind == QEvent.WindowStateChange and event.oldState() & Qt.WindowMaximized
                    and not _state(watched) & (Qt.WindowMinimized | Qt.WindowMaximized)):
                self._restore_normal_size(watched)
            if kind == QEvent.WindowStateChange:
                if self._owner_minimized():
                    if not event.oldState() & Qt.WindowMinimized:
                        self._note_minimized(self.owner)
                    self._suspend()
                else:
                    self._forget_minimized(self.owner)
                    if event.oldState() & Qt.WindowMinimized:
                        QTimer.singleShot(0, lambda: _refresh_restored_window(self.owner))
                    if self.suspended:
                        QTimer.singleShot(0, self._resume)
            elif kind == QEvent.WindowActivate:
                # A maximized child used to stay above its owner, so clicking the
                # main window never brought it forward. Reveal it after Qt applies
                # the activation.
                self._active_window = self.owner
                self._queue_taskbar()
                QTimer.singleShot(0, self._reveal_owner)
            elif kind == QEvent.Show:
                self._queue_taskbar(force=True)
            elif kind == QEvent.Hide and getattr(self.owner, "_closing", False):
                self._suspend()
            if kind in (QEvent.Move, QEvent.Resize, QEvent.Show):
                self._capture_owner_geometry()
            return False
        if (not isinstance(watched, QDialog) or not isValid(watched) or not self._belongs(watched)
                or self._native_file_dialog(watched)):
            return False
        record = self._record(watched)
        if (kind == QEvent.WindowStateChange and event.oldState() & Qt.WindowMaximized
                and not _state(watched) & (Qt.WindowMinimized | Qt.WindowMaximized)):
            self._restore_normal_size(watched)
        if kind == QEvent.Close:
            record.closing = True
            QTimer.singleShot(0, lambda window=watched: self._finish_close(window))
        elif kind == QEvent.Show:
            record.closed = False
            record.closing = False
            record.visible = True
            if self.suspended or self._owner_minimized():
                self._save(watched, record)
                QTimer.singleShot(0, self._suspend_if_needed)
            else:
                record.state = _state(watched)
            self._queue_taskbar(force=True)
        elif kind == QEvent.WindowActivate:
            self._active_window = watched
            self._queue_taskbar()
        elif kind in (QEvent.Hide, QEvent.WindowStateChange):
            if (kind == QEvent.WindowStateChange and not watched.isMinimized()
                    and watched in self._minimized_order):
                self._forget_minimized(watched)
            if (kind == QEvent.WindowStateChange and watched.isMinimized()
                    and not event.oldState() & Qt.WindowMinimized and not self.suspended
                    and not self._owner_minimized()):
                self._note_minimized(watched)
            if self._owner_minimized():
                self._suspend()
            elif not self.suspended:
                if kind == QEvent.Hide:
                    record.visible = False
                else:
                    record.state = _state(watched)
            self._queue_taskbar()
        if kind in (QEvent.Move, QEvent.Resize, QEvent.Show) and not self.suspended and not watched.isMinimized():
            self._capture_geometry(watched)
            # A Show/Move filter runs before Qt finishes applying frame offsets.
            QTimer.singleShot(0, lambda window=watched: self._capture_geometry(window))
        return False
