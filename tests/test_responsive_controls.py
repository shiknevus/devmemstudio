"""Responsive display changes must not alter connection or register parameters."""
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QLabel
from devmem_studio.core import ConfigStore
from devmem_studio.theme import STYLE
from devmem_studio.widgets import ElidedLabel
from devmem_studio.window import MainWindow


class ResponsiveControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("msyh.ttc", "consola.ttf"):
            if (fonts / name).is_file():
                QFontDatabase.addApplicationFont(str(fonts / name))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = MainWindow(ConfigStore(Path(self.temp.name) / "settings.json"), persist=False)
        self.window.show()
        self.app.processEvents()
        self.window.poll_timer.stop()
        self.window.heartbeat.stop()

    def tearDown(self):
        self.window._busy = False
        self.window._serial_busy = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.temp.cleanup()

    def resize(self, width, height):
        self.window.resize(width, height)
        for _ in range(8):
            self.app.processEvents()

    def test_channel_dots_carry_state_and_full_text(self):
        self.resize(1280, 800)
        sections = (self.window.ssh_section, self.window.serial_section)
        self.assertEqual([section.dot.toolTip() for section in sections], ["SSH 离线", "serial 离线"])
        self.window.connected = True
        self.window.serial_connected = True
        self.window._update_connection_status()
        self.assertEqual([section.dot.property("state") for section in sections], ["connected", "connected"])
        self.assertEqual([section.dot.toolTip() for section in sections], ["SSH 已连接", "serial 已连接"])

    def test_channel_lines_keep_their_geometry_across_states_and_sizes(self):
        for size in ((1540, 960), (1280, 800), (960, 620)):
            self.resize(*size)
            heights = None
            for connected, busy, demo, serial, serial_busy in (
                    (False, False, False, False, False),
                    (False, True, False, False, True),
                    (True, False, False, True, False),
                    (True, False, True, False, False)):
                self.window.connected, self.window._busy, self.window.demo = connected, busy, demo
                self.window._task_kind = "connect" if busy else ""
                self.window.serial_connected, self.window._serial_busy = serial, serial_busy
                self.window._update_connection_status()
                self.app.processEvents()
                current = (self.window.ssh_section.toggle.height(), self.window.serial_section.toggle.height())
                heights = heights or current
                self.assertEqual(current, heights)
                self.assertTrue(self.window.connect_button.isVisible())
                self.assertTrue(self.window.serial_connect_button.isVisible())
                for section in (self.window.ssh_section, self.window.serial_section):
                    self.assertTrue(section.rect().contains(section.toggle.geometry()))

    def assert_rail_columns_aligned(self):
        side = self.window.sidebar_content

        def span(widget):
            left = widget.mapTo(side, widget.rect().topLeft()).x()
            return left, left + widget.width()

        card = self.window.ssh_section.parentWidget()
        self.assertEqual(span(self.window.component_tree), span(card))
        self.assertEqual(span(self.window.component_search), span(card))
        self.assertEqual(span(self.window.base_field)[1], span(card)[1])

    def test_component_tree_matches_rail_width_after_compact_start(self):
        self.resize(1280, 800)
        self.assert_rail_columns_aligned()
        self.resize(1920, 1080)
        self.assertGreaterEqual(self.window.sidebar_scroll.width(), 320)
        self.assert_rail_columns_aligned()
        self.window.side_split.setSizes([320, 1591])
        self.app.processEvents()
        self.assert_rail_columns_aligned()

    def assert_filter_bar_fits(self, tag):
        for _ in range(8):
            self.app.processEvents()
        bar, combo, search = self.window.register_filters, self.window.access_filter, self.window.search
        last_tab = max(tab.geometry().right() for tab in self.window.view_buttons.values())
        self.assertLess(last_tab, combo.geometry().left(), tag)
        self.assertLess(combo.geometry().right(), search.geometry().left(), tag)
        self.assertLessEqual(search.geometry().right(), bar.width(), tag)
        for tab in self.window.view_buttons.values():
            self.assertGreaterEqual(tab.width(), tab.minimumSizeHint().width(), tag)

    def test_register_filter_bar_never_overlaps(self):
        for size in ((2400, 1300), (1540, 960), (1420, 960), (1300, 800), (1200, 800),
                     (1180, 740), (1100, 800), (960, 620)):
            self.resize(*size)
            self.assert_filter_bar_fits(f"window {size}")
        self.resize(1540, 960)
        self.window.side_split.setSizes([620, 911])
        self.assert_filter_bar_fits("rail dragged wide")
        self.window.side_split.setSizes([360, 1171])
        total = sum(self.window.horizontal_split.sizes())
        self.window.horizontal_split.setSizes([total - 350, 350])
        self.assert_filter_bar_fits("inspector dragged wide")
        self.resize(1200, 800)
        self.assertEqual(self.window.view_buttons["irq"].text(), "中断")   # full labels before single letters

    def test_badge_shows_full_text_at_its_size_hint(self):
        badge = ElidedLabel("ec_slv_pul_axis", "badge")
        badge.resize(badge.sizeHint())
        badge.show()
        self.app.processEvents()
        self.assertEqual(QLabel.text(badge), "ec_slv_pul_axis")
        badge.resize(badge.sizeHint().width() - 30, badge.height())
        self.app.processEvents()
        self.assertTrue(QLabel.text(badge).endswith("…"))
        self.assertEqual(badge.text(), "ec_slv_pul_axis")
        badge.close()

    def test_title_row_keeps_long_component_names_whole(self):
        name = "ec_superisys_485_modbus_rtu_27"
        title, badge = self.window.module_title, self.window.module_badge
        title.setText(f"{name} · 寄存器映射")
        badge.setText("ec_superisys_485_modbus_rtu")
        self.window._set_register_count("10 / 33 项 · 全地址 0xB0106C00")
        for size in ((2560, 1440), (1920, 1080), (1540, 960), (1280, 800), (1180, 740), (960, 620)):
            self.resize(*size)
            shown = QLabel.text(title)
            if size[0] >= 1540:
                self.assertEqual(shown, title.text(), size)   # maximized / desktop: nothing elided
                self.assertEqual(QLabel.text(badge), badge.text(), size)
            elif size[0] >= 1180:
                self.assertTrue(shown.startswith(name), size)   # the badge and suffix give way first
            row = [widget for widget in (title, self.window.register_count, badge, self.window.poll_check,
                                         self.window.interval, self.window.write_all_button,
                                         self.window.read_all_button, self.window.export_button,
                                         self.window.help_button) if widget.isVisible()]
            for left, right in zip(row, row[1:]):
                self.assertLess(left.geometry().right(), right.geometry().left(), size)

    def test_log_selectors_restore_width_and_keep_selection_and_source(self):
        self.resize(1540, 960)
        self.window.log_filter.setCurrentIndex(1)
        self.window.console_source_combo.setCurrentIndex(self.window.console_source_combo.findData("com"))
        self.resize(1280, 800)
        self.assertEqual(self.window.log_filter.currentText(), "错误")
        self.assertEqual(self.window.log_filter.toolTip(), "仅错误")
        self.assertEqual(self.window.console_source_combo.currentData(), "com")
        self.assertEqual(self.window.console_source, "com")
        self.assertIn("串口终端", self.window.console_source_combo.toolTip())
        self.assertEqual(self.window.log_filter.width(), 88)
        self.resize(1540, 960)
        self.assertEqual(self.window.log_filter.currentText(), "仅错误")
        self.assertEqual(self.window.log_filter.width(), 118)
        self.assertEqual(self.window.console_source_combo.width(), 118)
        self.assertEqual(self.window.console_source, "com")

    def test_log_source_tooltip_tracks_each_selected_channel_in_tiny_mode(self):
        self.resize(960, 620)
        for key, text in (("log", "板端 sunny.log 流"), ("ssh", "SSH 会话日志"),
                          ("com", "串口终端"), ("system", "软件运行日志")):
            self.window.console_source_combo.setCurrentIndex(self.window.console_source_combo.findData(key))
            self.assertEqual(self.window.console_source_combo.currentData(), key)
            self.assertEqual(self.window.console_source, key)
            self.assertIn(text, self.window.console_source_combo.toolTip())
        self.assertEqual(self.window.console_source_combo.currentText(), "sys")
        self.assertEqual(self.window.console_source_combo.width(), 70)

    def test_access_selector_abbreviates_and_preserves_selected_permission(self):
        self.resize(1540, 960)
        self.window.access_filter.setCurrentIndex(2)
        self.resize(1280, 800)
        self.assertEqual(self.window.access_filter.currentText(), "读写")
        self.assertEqual(self.window.access_filter.currentIndex(), 2)
        self.assertEqual(self.window.access_filter.toolTip(), "可读写")
        self.assertEqual(self.window.access_filter.itemData(0, Qt.ToolTipRole), "全部权限")
        self.resize(1540, 960)
        self.assertEqual(self.window.access_filter.currentText(), "可读写")
        self.assertEqual(self.window.access_filter.width(), 112)

    def test_interval_preset_label_changes_but_value_does_not(self):
        self.resize(1540, 960)
        self.window.interval.setCurrentIndex(self.window.interval.findData(200))
        self.resize(1280, 800)
        self.assertEqual(self.window.interval.currentText(), "0.2s")
        self.assertEqual(self.window.interval.currentData(), 200)
        self.assertEqual(self.window._poll_interval_ms(), 200)
        self.resize(1540, 960)
        self.assertEqual(self.window.interval.currentText(), "0.2 s")
        self.assertEqual(self.window._poll_interval_ms(), 200)

    def test_interval_custom_draft_and_cursor_survive_resizing(self):
        self.resize(1540, 960)
        self.window.interval.setEditText("150ms")
        self.window.interval.lineEdit().setCursorPosition(2)
        self.resize(1280, 800)
        self.assertEqual(self.window.interval.currentText(), "150ms")
        self.assertEqual(self.window.interval.lineEdit().cursorPosition(), 2)
        self.assertEqual(self.window._poll_interval_ms(), 150)
        self.resize(1540, 960)
        self.assertEqual(self.window.interval.currentText(), "150ms")
        self.assertEqual(self.window.interval.lineEdit().cursorPosition(), 2)

    def test_serial_choice_keeps_actual_port_and_complete_description(self):
        self.resize(1540, 960)
        self.window.serial_port.clear()
        label = self.window._serial_port_label("COM9", "USB Serial Port (COM9)")
        self.assertEqual(label, "COM9 · USB Serial Port")
        self.window.serial_port.addItem(label, "COM9")
        self.window.serial_baud.setCurrentText("57600")
        self.resize(1280, 800)
        self.assertEqual(self.window.serial_port.currentText(), label)
        self.assertEqual(self.window.serial_port.currentData(), "COM9")
        self.assertEqual(self.window._selected_serial_port(), "COM9")
        self.assertIn("USB Serial Port", self.window.serial_port.toolTip())
        self.assertEqual(self.window.serial_baud.currentText(), "57600")
        self.assertEqual(self.window._serial_baud_value(), 57600)
        self.assertEqual(self.window.serial_section.summary.text(), "COM9 · 57600")
        self.resize(1540, 960)
        self.assertEqual(self.window.serial_port.currentText(), label)
        self.assertEqual(self.window._serial_port_label("COM3", ""), "COM3")

    def test_tiny_interval_preset_is_fully_visible_not_only_its_unit(self):
        self.resize(1540, 960)
        self.window.interval.setCurrentIndex(self.window.interval.findData(100))
        self.resize(960, 620)
        edit = self.window.interval.lineEdit()
        self.assertEqual(self.window.interval.currentText(), "0.1s")
        self.assertGreaterEqual(edit.contentsRect().width(),
                                edit.fontMetrics().horizontalAdvance(edit.text()))

    def test_tiny_view_selectors_abbreviate_and_preserve_selected_view(self):
        self.resize(1540, 960)
        self.window.view_buttons["irq"].click()
        self.resize(960, 620)
        self.assertEqual(self.window.view_buttons["irq"].text(), "IRQ")
        self.assertEqual(self.window.view_buttons["basic"].text(), "基")
        self.assertIn("中断", self.window.view_buttons["irq"].toolTip())
        self.assertTrue(self.window.view_buttons["irq"].isChecked())
        for tab in self.window.view_buttons.values():
            self.assertGreaterEqual(tab.width(), tab.minimumSizeHint().width())
        self.resize(1540, 960)
        self.assertEqual(self.window.view_buttons["irq"].text(), "中断")
        self.assertTrue(self.window.view_buttons["irq"].isChecked())

    def test_register_editor_selectors_keep_values_and_full_tooltips(self):
        self.resize(1540, 960)
        self.window.width_combo.setCurrentText("64")
        self.window.format_combo.setCurrentIndex(1)
        self.window.preset_combo.addItem("较长的行为预设名称（原始定义保留）", 42)
        self.window.preset_combo.setCurrentIndex(self.window.preset_combo.findData(42))
        self.resize(1280, 800)
        self.assertEqual(self.window.width_combo.currentText(), "64")
        self.assertEqual(self.window.format_combo.currentData(), "D")
        self.assertEqual(self.window.preset_combo.currentData(), 42)
        self.assertEqual(self.window.format_combo.toolTip(), "十进制（DEC）")
        self.assertIn("原始定义保留", self.window.preset_combo.toolTip())
        self.assertEqual(self.window.width_combo.width(), 64)
        self.assertEqual(self.window.format_combo.width(), 64)
        self.resize(1540, 960)
        self.assertEqual(self.window.width_combo.width(), 80)
        self.assertEqual(self.window.format_combo.width(), 88)


if __name__ == "__main__":
    unittest.main()
