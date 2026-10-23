"""Owner minimize/restore must preserve every dialog's independent state."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest
from PySide6.QtCore import QPoint, QRect
from devmem_studio.window_state import ManagedDialog, WindowStateCoordinator, saved_origin


class WindowStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def settle(self):
        for _ in range(8):
            self.app.processEvents()

    def setUp(self):
        self.owner = QWidget()
        self.owner.cfg = {}
        self.owner.resize(800, 600)
        self.owner._window_states = WindowStateCoordinator(self.owner)
        self.owner.show()
        self.settle()

    def tearDown(self):
        for child in self.owner.findChildren(ManagedDialog):
            child.close()
        self.owner.close()
        self.owner.deleteLater()
        self.settle()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def dialog(self):
        dialog = ManagedDialog(self.owner)
        dialog.resize(520, 360)
        dialog.move(70, 90)
        dialog.present(activate=False)
        self.settle()
        return dialog

    def test_dialog_is_a_real_window_with_close_minimize_and_maximize_buttons(self):
        dialog = self.dialog()
        self.assertEqual(dialog.windowFlags() & Qt.WindowType_Mask, Qt.Window)
        self.assertTrue(dialog.windowFlags() & Qt.WindowMinimizeButtonHint)
        self.assertTrue(dialog.windowFlags() & Qt.WindowMaximizeButtonHint)
        self.assertTrue(dialog.windowFlags() & Qt.WindowCloseButtonHint)
        self.assertTrue(dialog.windowFlags() & Qt.WindowSystemMenuHint)

    def test_owner_cycles_restore_normal_geometry_and_maximized_child(self):
        normal, maximized = self.dialog(), self.dialog()
        geometry = normal.geometry()
        maximized.showMaximized()
        self.settle()
        for restore in (self.owner.showNormal, self.owner.showMaximized, self.owner.showNormal):
            self.owner.showMinimized()
            self.settle()
            self.assertFalse(normal.isVisible())
            self.assertFalse(maximized.isVisible())
            restore()
            self.settle()
            self.assertTrue(normal.isVisible())
            self.assertEqual(normal.windowState() & (Qt.WindowMinimized | Qt.WindowMaximized), Qt.WindowNoState)
            self.assertEqual(normal.geometry(), geometry)
            self.assertTrue(maximized.isVisible())
            self.assertTrue(maximized.isMaximized())
            self.assertFalse(maximized.isMinimized())

    def test_independently_minimized_child_stays_minimized_and_reopens_maximized(self):
        child = self.dialog()
        child.showMaximized()
        self.settle()
        child.showMinimized()
        self.settle()
        self.owner.showMinimized()
        self.settle()
        self.owner.showNormal()
        self.settle()
        self.assertTrue(child.isMinimized())
        child.present(activate=False)
        self.settle()
        self.assertTrue(child.isMaximized())
        self.assertFalse(child.isMinimized())

    def test_closed_or_unopened_child_is_not_resurrected(self):
        closed, hidden = self.dialog(), ManagedDialog(self.owner)
        closed.close()
        self.owner.showMinimized()
        self.settle()
        self.owner.showNormal()
        self.settle()
        self.assertFalse(closed.isVisible())
        self.assertFalse(hidden.isVisible())

    def test_close_during_owner_minimize_is_not_undone(self):
        child = self.dialog()
        self.owner.showMinimized()
        self.settle()
        child.close()
        self.owner.showNormal()
        self.settle()
        self.assertFalse(child.isVisible())

    def assert_centred_on_owner(self, dialog):
        offset = self.owner.geometry().center() - dialog.geometry().center()
        self.assertLessEqual(abs(offset.x()) + abs(offset.y()), 60, (offset, dialog.geometry()))   # Qt's frame estimate

    def test_first_present_centres_even_with_an_early_native_window(self):
        self.owner.resize(500, 400)
        self.owner.move(100, 100)
        self.settle()
        dialog = ManagedDialog(self.owner)
        dialog.resize(300, 200)
        dialog.winId()   # created before the first present, like BitPackDialog
        dialog.present(activate=False)
        self.settle()
        self.assert_centred_on_owner(dialog)   # was pinned at (0, 0): top-left of the screen
        self.assertEqual(dialog.size().toTuple(), (300, 200))
        dialog.move(150, 160)
        self.settle()
        dialog.close()
        self.settle()
        dialog.present(activate=False)
        self.settle()
        self.assertEqual(dialog.pos().toTuple(), (150, 160))   # a reopened window keeps its own place

    def test_first_present_while_owner_minimized_is_centred_on_restore(self):
        self.owner.resize(500, 400)
        self.owner.move(100, 100)
        self.settle()
        self.owner.showMinimized()
        self.settle()
        dialog = ManagedDialog(self.owner)
        dialog.resize(300, 200)
        dialog.winId()
        dialog.present(activate=False)
        self.settle()
        self.owner.showNormal()
        self.settle()
        self.assertTrue(dialog.isVisible())
        self.assert_centred_on_owner(dialog)

    def test_present_while_owner_minimized_waits_for_owner_restore(self):
        self.owner.showMinimized()
        self.settle()
        child = ManagedDialog(self.owner)
        child.present(activate=False)
        self.settle()
        self.assertTrue(self.owner.isMinimized())
        self.assertFalse(child.isVisible())
        self.owner.showNormal()
        self.settle()
        self.assertTrue(child.isVisible())
        self.assertFalse(child.isMinimized())

    def test_late_suspend_request_does_not_hide_restored_windows(self):
        child = self.dialog()
        self.owner.showMinimized()
        self.settle()
        self.owner.showNormal()
        QTimer.singleShot(0, self.owner._window_states._suspend_if_needed)
        self.settle()
        self.assertTrue(child.isVisible())
        self.assertFalse(self.owner._window_states.suspended)

    def test_late_native_minimize_notification_keeps_original_maximized_state(self):
        child = self.dialog()
        child.showMaximized()
        self.settle()
        self.owner.showMinimized()
        self.settle()
        self.owner.showNormal()
        QTimer.singleShot(0, child.showMinimized)
        self.settle()
        self.assertTrue(child.isMaximized())
        self.assertFalse(child.isMinimized())

    def test_owner_activation_does_not_hide_a_maximized_child(self):
        child = self.dialog()
        child.showMaximized()
        self.settle()
        QApplication.sendEvent(self.owner, QEvent(QEvent.WindowActivate))
        self.owner._window_states._reveal_owner()
        self.settle()
        self.assertTrue(self.owner.isVisible())
        self.assertFalse(self.owner.isMinimized())
        self.assertTrue(child.isVisible())
        self.assertTrue(child.isMaximized())

    def test_restore_does_not_activate_child_before_owner(self):
        observations = []

        class Child(ManagedDialog):
            def showEvent(inner, event):
                observations.append((inner.parentWidget().isMinimized(), inner.parentWidget().isVisible(),
                                     inner.testAttribute(Qt.WA_ShowWithoutActivating)))
                super().showEvent(event)

        child = Child(self.owner)
        child.show()
        self.settle()
        observations.clear()
        self.owner.showMinimized()
        self.settle()
        self.owner.showNormal()
        self.settle()
        self.assertEqual(observations, [(False, True, True)])

    def tabs(self):
        shown, hidden = self.owner._window_states._taskbar_tabs()
        return set(shown), set(hidden)

    def test_minimized_children_keep_folded_tabs_and_native_restore_preserves_maximized_state(self):
        first, last = self.dialog(), self.dialog()
        first.showMinimized()
        self.settle()
        last.showMaximized()
        self.settle()
        last.showMinimized()
        self.settle()
        # Every window keeps its own tab; Windows folds them into one app button.
        self.assertEqual(self.tabs(), ({self.owner, first, last}, set()))
        last.present(activate=False)
        self.settle()
        self.assertTrue(first.isMinimized())
        self.assertFalse(last.isMinimized())
        self.assertTrue(last.isMaximized())

    def test_minimized_owner_is_the_only_tab_until_restored(self):
        child = self.dialog()
        child.showMinimized()
        self.settle()
        self.owner.showMinimized()
        self.settle()
        self.assertEqual(self.tabs(), ({self.owner}, {child}))
        self.owner.showNormal()
        self.settle()
        self.assertFalse(self.owner.isMinimized())
        self.assertTrue(child.isMinimized())
        self.assertEqual(self.tabs(), ({self.owner, child}, set()))

    def test_closed_minimized_child_loses_its_tab(self):
        child = self.dialog()
        child.showMinimized()
        self.settle()
        child.close()
        self.settle()
        self.assertFalse(child.isVisible())
        self.assertEqual(self.tabs(), ({self.owner}, {child}))

    def test_owner_activation_never_restores_a_minimized_child(self):
        child = self.dialog()
        child.showMinimized()
        self.settle()
        QTest.qWait(180)
        QApplication.sendEvent(self.owner, QEvent(QEvent.WindowActivate))
        self.settle()
        self.assertTrue(child.isMinimized())
        self.assertEqual(self.tabs(), ({self.owner, child}, set()))

    def test_minimizing_owner_after_child_is_never_intercepted(self):
        child = self.dialog()
        child.showMinimized()
        self.settle()
        self.owner.showMinimized()
        self.settle()
        QTest.qWait(180)
        self.assertTrue(self.owner.isMinimized())
        self.assertFalse(child.isVisible())
        self.assertEqual(self.tabs(), ({self.owner}, {child}))

    def test_closing_or_destroying_minimized_child_keeps_other_tabs(self):
        first, last = self.dialog(), self.dialog()
        first.showMinimized()
        self.settle()
        last.showMinimized()
        self.settle()
        last.close()
        self.settle()
        self.assertEqual(self.tabs(), ({self.owner, first}, {last}))
        first.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.settle()
        self.assertEqual(self.tabs(), ({self.owner}, {last}))

    def test_repeated_main_and_child_restore_cycles_keep_geometry_and_no_auto_restore(self):
        child = self.dialog()
        geometry = child.geometry()
        for _ in range(4):
            child.showMinimized()
            self.settle()
            self.assertEqual(self.tabs(), ({self.owner, child}, set()))
            self.owner.showMinimized()
            self.settle()
            self.assertEqual(self.tabs(), ({self.owner}, {child}))
            self.owner.showNormal()
            self.settle()
            self.assertTrue(child.isMinimized())
            self.assertEqual(self.tabs(), ({self.owner, child}, set()))
            child.present(activate=False)
            self.settle()
            self.assertEqual(child.geometry(), geometry)
            self.assertFalse(child.isMinimized())

    def test_ignored_close_keeps_minimized_child_tab(self):
        class BusyDialog(ManagedDialog):
            def closeEvent(inner, event):
                event.ignore()

        child = BusyDialog(self.owner)
        child.present(activate=False)
        self.settle()
        child.showMinimized()
        self.settle()
        child.close()
        self.settle()
        self.assertTrue(child.isMinimized())
        self.assertIn(child, self.tabs()[0])

    def test_close_during_owner_restore_does_not_leave_a_closed_child_tab(self):
        child = self.dialog()
        child.showMinimized()
        self.settle()
        self.owner.showMinimized()
        self.settle()
        self.owner.showNormal()
        QTimer.singleShot(0, child.close)
        self.settle()
        self.assertFalse(child.isVisible())
        self.assertEqual(self.tabs(), ({self.owner}, {child}))

    def test_maximized_owner_saves_and_restores_its_normal_size(self):
        normal = self.owner.size()
        self.owner.showMaximized()
        self.settle()
        self.owner._window_states.save_window_preferences()
        saved = self.owner.cfg["window_size"]
        self.assertEqual(saved, [normal.width(), normal.height()])
        self.owner.showNormal()
        self.settle()
        self.assertEqual(self.owner.size(), normal)

    def test_maximized_child_saves_and_restores_its_normal_size(self):
        child = self.dialog()
        normal = child.size()
        child.showMaximized()
        self.settle()
        self.owner._window_states.save_window_preferences()
        record = self.owner.cfg["dialog_windows"][type(child).__name__]
        self.assertEqual(record["size"], [normal.width(), normal.height()])
        child.showNormal()
        self.settle()
        self.assertEqual(child.size(), normal)

    def test_owner_shown_maximized_saves_its_restore_position(self):
        owner = QWidget()
        owner.cfg = {}
        owner.resize(500, 400)
        owner._window_states = WindowStateCoordinator(owner)
        try:
            owner.setGeometry(QRect(130, 140, 500, 400))
            owner.setWindowState(Qt.WindowMaximized)
            owner.show()
            self.settle()
            owner._window_states.save_window_preferences()
            self.assertEqual(owner.cfg["window_pos"], [130, 140])
            self.assertEqual(owner.cfg["window_size"], [500, 400])
            self.assertTrue(owner.cfg["window_maximized"])
        finally:
            owner.close()
            owner.deleteLater()

    def test_saved_origin_requires_a_visible_title_bar(self):
        area = QApplication.primaryScreen().availableGeometry()
        size = [400, 300]
        inside = [area.left() + 40, area.top() + 60]
        self.assertEqual(saved_origin(inside, size), QPoint(*inside))
        self.assertIsNone(saved_origin([area.left() + 40, area.top()], size))   # title bar above the screen
        self.assertIsNone(saved_origin([area.right() + 50, area.top() + 60], size))
        self.assertIsNone(saved_origin([area.left() - 390, area.top() + 60], size))   # only 10 px left
        self.assertEqual(saved_origin([area.left() - 200, area.top() + 60], size),
                         QPoint(area.left() - 200, area.top() + 60))
        for value in (None, [1], [1.0, 2], ["1", 2], [True, 60], [1, 2, 3], [40000, 60]):
            self.assertIsNone(saved_origin(value, size))

    def test_main_window_reopens_at_its_saved_position(self):
        import json
        import tempfile
        from pathlib import Path
        from devmem_studio.core import ConfigStore
        from devmem_studio.window import MainWindow
        area = QApplication.primaryScreen().availableGeometry()
        # The 800 px offscreen screen clamps the width; the saved height still fits.
        size = [area.width() - 16, area.height() - 160]
        origin = [area.left() + 8, area.top() + 50]   # the default placement centers it vertically
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            for saved, expected in ((origin, origin[1]), ([area.right() + 500, 50], None)):
                path.write_text(json.dumps({"window_size": size, "window_pos": saved}), encoding="utf-8")
                window = MainWindow(ConfigStore(path), persist=False)
                try:
                    window.show()
                    self.settle()
                    self.assertEqual(window.height(), size[1])
                    self.assertTrue(area.contains(window.frameGeometry()), window.frameGeometry())
                    if expected is not None:
                        self.assertEqual(window.geometry().y(), expected)
                    window._window_states.save_window_preferences()
                    client = window.geometry()   # client origin: x()/y() include the frame
                    self.assertEqual(window.cfg["window_pos"], [client.x(), client.y()])
                finally:
                    window.close()
                    window.deleteLater()
                    self.settle()

    def test_small_screen_lowers_the_main_window_floor_to_fit(self):
        import tempfile
        from pathlib import Path
        from devmem_studio.core import ConfigStore
        from devmem_studio.window import MainWindow, WINDOW_COMPACT_MIN_SIZE
        area = QApplication.primaryScreen().availableGeometry()
        self.assertLess(area.width(), WINDOW_COMPACT_MIN_SIZE[0])   # offscreen screen: 800 px wide
        with tempfile.TemporaryDirectory() as temp:
            window = MainWindow(ConfigStore(Path(temp) / "settings.json"), persist=False)
            try:
                window.show()
                self.settle()
                self.assertLessEqual(window.minimumWidth(), area.width() - 16)
                self.assertTrue(area.contains(window.frameGeometry()), window.frameGeometry())
            finally:
                window.close()
                window.deleteLater()
                self.settle()

    def test_saved_maximized_state_does_not_replace_normal_geometry(self):
        cfg = {"window_size": [640, 480], "window_maximized": True,
               "dialog_windows": {"ManagedDialog": {"size": [520, 360], "maximized": True}}}
        self.owner.cfg = cfg
        self.owner._window_states._owner_geometry = QRect(70, 90, 640, 480)
        child = ManagedDialog(self.owner)
        child.present(activate=False)
        child.showMaximized()
        self.owner._window_states.save_window_preferences()
        self.assertEqual(cfg["window_size"], [800, 600])
        self.assertEqual(cfg["dialog_windows"]["ManagedDialog"]["size"], [520, 360])
