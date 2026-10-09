"""Owner minimize/restore must preserve every dialog's independent state."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from devmem_studio.window_state import ManagedDialog, WindowStateCoordinator


class WindowStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def settle(self):
        for _ in range(8):
            self.app.processEvents()

    def setUp(self):
        self.owner = QWidget()
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
