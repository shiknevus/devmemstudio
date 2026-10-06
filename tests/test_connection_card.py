"""Connection card: one line per channel, fields fold inline, one-click connect."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea
from devmem_studio.core import CommandError, ConfigStore, DemoSession
from devmem_studio.serial_session import SerialSession
from devmem_studio.theme import STYLE
from devmem_studio.window import MainWindow


class ConnectionCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.close()
            self.settle(lambda: not self.window._busy and not self.window._serial_busy)
            self.window.deleteLater()
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.temp.cleanup()

    def settle(self, predicate=lambda: True, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("GUI did not settle")

    def open(self, **settings):
        path = Path(self.temp.name) / "settings.json"
        if settings:
            path.write_text(json.dumps(settings), encoding="utf-8")
        self.window = MainWindow(ConfigStore(path), persist=False)
        self.window.resize(1540, 960)
        self.window.show()
        self.window.poll_timer.stop()
        self.window.heartbeat.stop()   # keep injected COM ports from being rescanned away
        self.settle()
        return self.window

    def add_port(self, window, device="COM9"):
        window.serial_port.addItem(window._serial_port_label(device, f"USB Serial Port ({device})"), device)
        window.serial_port.setCurrentIndex(window.serial_port.findData(device))

    def test_first_run_shows_ssh_fields_and_folded_serial_line(self):
        window = self.open()
        self.assertTrue(window.ssh_section.is_expanded())
        self.assertTrue(window.host.isVisible())
        self.assertFalse(window.serial_section.is_expanded())
        self.assertFalse(window.serial_port.isVisible())
        self.assertTrue(window.serial_connect_button.isVisible())
        self.assertEqual(window.ssh_section.summary.text(), "未设置主板地址")
        self.assertEqual(window.ssh_section.dot.property("state"), "offline")
        self.assertEqual(window.sidebar_scroll.findChildren(QScrollArea), [])

    def test_saved_target_starts_folded_with_summary(self):
        window = self.open(host="192.168.1.10", username="root", port=2222)
        self.assertFalse(window.ssh_section.is_expanded())
        self.assertFalse(window.serial_section.is_expanded())
        self.assertTrue(window.connect_button.isVisible())
        self.assertEqual(window.connect_button.text(), "连接")
        self.assertEqual(window.ssh_section.summary.text(), "192.168.1.10:2222")
        self.assertIn("root@192.168.1.10:2222", window.ssh_section.toggle.toolTip())
        window.port.setValue(22)
        self.assertEqual(window.ssh_section.summary.text(), "192.168.1.10")

    def test_channels_open_one_at_a_time(self):
        window = self.open(host="192.168.1.10")
        window.ssh_section.toggle.click()
        self.assertTrue(window.ssh_section.is_expanded())
        self.assertTrue(window.host.isVisible())
        window.serial_section.toggle.click()
        self.assertTrue(window.serial_section.is_expanded())
        self.assertFalse(window.ssh_section.is_expanded())
        self.assertFalse(window.host.isVisible())
        window.serial_section.toggle.click()
        self.assertFalse(window.serial_section.is_expanded())

    def test_connect_without_address_opens_ssh_fields(self):
        window = self.open()
        window.ssh_section.set_expanded(False)
        window.connect_button.click()
        self.assertTrue(window.ssh_section.is_expanded())
        self.assertFalse(window.connected)
        self.assertTrue(any("主机地址" in text for _, _, text in window.log_records_system))

    def test_enter_connects_and_success_folds_the_channel(self):
        window = self.open(host="192.0.2.10", username="root")
        window.ssh_section.toggle.click()
        with patch("devmem_studio.window.SshSession", DemoSession):
            QTest.keyClick(window.password, Qt.Key_Return)
            self.settle(lambda: window.connected and not window._busy)
        self.assertFalse(window.ssh_section.is_expanded())
        self.assertEqual(window.connect_button.text(), "断开")
        self.assertEqual(window.connect_button.objectName(), "danger")
        self.assertEqual(window.ssh_section.dot.property("state"), "demo")
        self.assertEqual(window.ssh_section.summary.text(), "本地模拟设备")
        window.connect_button.click()
        self.assertFalse(window.connected)
        self.assertEqual(window.connect_button.text(), "连接")
        self.assertEqual(window.ssh_section.dot.property("state"), "offline")
        self.assertEqual(window.ssh_section.summary.text(), "192.0.2.10")

    def test_failed_connect_reopens_fields_but_cancel_does_not(self):
        window = self.open(host="192.0.2.10", username="root")

        class Refused(DemoSession):
            def connect(self, *args, **kwargs):
                raise OSError("connection refused")

        class Cancelled(DemoSession):
            def connect(self, *args, **kwargs):
                raise CommandError("连接已取消。")

        with patch("devmem_studio.window.SshSession", Refused):
            window.connect_button.click()
            self.settle(lambda: not window._busy)
        self.assertTrue(window.ssh_section.is_expanded())
        self.assertEqual(window.connect_button.text(), "连接")
        self.assertEqual(window.ssh_section.dot.property("state"), "offline")
        window.ssh_section.set_expanded(False)
        with patch("devmem_studio.window.SshSession", Cancelled):
            window.connect_button.click()
            self.settle(lambda: not window._busy)
        self.assertFalse(window.ssh_section.is_expanded())

    def test_serial_line_tracks_connecting_and_connected_states(self):
        window = self.open()
        release = threading.Event()

        class SlowSerial(SerialSession):
            def connect(self, port, baud, username, password, timeout):
                release.wait(5)
                self._test_alive = True

            @property
            def alive(self):
                return getattr(self, "_test_alive", False)

            def close(self):
                self._test_alive = False

        with patch("devmem_studio.window.SerialSession", SlowSerial):
            self.add_port(window)
            window.serial_section.toggle.click()
            QTest.keyClick(window.serial_password, Qt.Key_Return)
            self.settle(lambda: window._serial_busy)
            self.assertEqual(window.serial_section.dot.property("state"), "connecting")
            self.assertEqual(window.serial_connect_button.text(), "取消")
            release.set()
            self.settle(lambda: window.serial_connected and not window._serial_busy)
        self.assertEqual(window.serial_section.dot.property("state"), "connected")
        self.assertEqual(window.serial_connect_button.text(), "断开")
        self.assertEqual(window.serial_section.summary.text(), "COM9 · 115200")
        self.assertFalse(window.serial_section.is_expanded())
        window.disconnect_serial()
        self.assertEqual(window.serial_section.dot.property("state"), "offline")

    def test_failed_serial_connect_reopens_serial_fields(self):
        window = self.open()

        class Broken(SerialSession):
            def connect(self, port, baud, username, password, timeout):
                raise CommandError("用户名或密码错误")

            def close(self):
                pass

        with patch("devmem_studio.window.SerialSession", Broken):
            self.add_port(window)
            window.serial_connect_button.click()
            self.settle(lambda: not window._serial_busy and window.serial_section.is_expanded())
        self.assertFalse(window.serial_connected)
        self.assertFalse(window.ssh_section.is_expanded())

    def test_port_list_shows_number_first_and_empty_list_hint(self):
        window = self.open()
        window.serial_port.clear()
        self.assertEqual(window.serial_section.summary.text(), "未检测到串口")
        window.serial_port.addItem(window._serial_port_label("COM35", "Prolific PL2303GT USB Serial COM Port (COM35)"),
                                   "COM35")
        self.assertEqual(window.serial_port.currentText(), "COM35 · Prolific PL2303GT USB Serial COM Port")
        self.assertEqual(window.serial_section.summary.text(), "COM35 · 115200")
        self.assertFalse(window.serial_port.grab().isNull())   # elided closed-state paint


if __name__ == "__main__":
    unittest.main()
