"""Log readability, keyboard navigation and behavior while live output changes."""
import os
from pathlib import Path
import time
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from devmem_studio.dialogs import BoardLogDialog
from devmem_studio.theme import STYLE


class LogViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("msyh.ttc", "consola.ttf"):
            if (fonts / filename).exists():
                QFontDatabase.addApplicationFont(str(fonts / filename))

    def setUp(self):
        self.dialog = BoardLogDialog("/run/media/sda/sunny.log")
        self.dialog.show()
        self.dialog.activateWindow()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def settle(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Log view did not settle")

    def color_at(self, block_number, text):
        block = self.dialog.text.document().findBlockByNumber(block_number)
        prefix = block.text().split(text, 1)[0]
        offset = len(prefix.encode("utf-16-le")) // 2
        for span in block.layout().formats():
            if span.start <= offset < span.start + span.length:
                return span.format.foreground().color().name()
        return None

    def search(self, query, count):
        self.dialog.search.setText(query)
        self.settle(lambda: not self.dialog.search_timer.isActive() and self.dialog.match_count.text() == count)

    def test_severity_timestamps_addresses_and_split_lines_preserve_plain_text(self):
        prefix = "10:20:30.100 😀 ER"
        suffix = "ROR 写入失败 addr=0xB0102208\n10:20:30 WARN 温度告警\nSUCCESS 回读成功\nDEBUG cycle=1\n"
        self.dialog.append(prefix)
        self.dialog.append(suffix)
        self.app.processEvents()
        self.assertEqual(self.dialog.text.toPlainText(), prefix + suffix)
        self.assertEqual(self.color_at(0, "ERROR"), "#ff8e94")
        self.assertEqual(self.color_at(0, "10:20:30.100"), "#809cb3")
        self.assertEqual(self.color_at(0, "0xB0102208"), "#87bcff")
        self.assertEqual(self.color_at(1, "WARN"), "#f0c478")
        self.assertEqual(self.color_at(2, "SUCCESS"), "#7edbbd")
        self.assertEqual(self.color_at(3, "DEBUG"), "#8da4b8")

    def test_literal_unicode_search_case_toggle_navigation_and_wrap(self):
        self.dialog.append("😀 [err].* 测试\n[ERR].* 测试\n[err].* 😀 测试\n")
        self.search("[err].*", "1 / 3")
        self.assertEqual(self.dialog.text.current_match.selectionStart(), 3)
        self.assertEqual(self.dialog.text.current_match.selectedText(), "[err].*")
        self.dialog.next_button.click()
        self.assertEqual(self.dialog.match_count.text(), "2 / 3")
        self.dialog.next_button.click()
        self.dialog.next_button.click()
        self.assertEqual(self.dialog.match_count.text(), "1 / 3")
        self.dialog.previous_button.click()
        self.assertEqual(self.dialog.match_count.text(), "3 / 3")
        self.dialog.case_sensitive.setChecked(True)
        self.settle(lambda: not self.dialog.search_timer.isActive())
        self.assertTrue(self.dialog.match_count.text().endswith("/ 2"))
        self.search("不存在", "0 / 0")
        self.assertFalse(self.dialog.next_button.isEnabled())
        self.assertFalse(self.dialog.previous_button.isEnabled())
        self.search("😀", "2 / 2")
        self.assertEqual(self.dialog.text.current_match.selectedText(), "😀")

    def test_keyboard_find_enter_previous_and_escape_do_not_control_stream(self):
        starts, stops = [], []
        self.dialog.start_requested.connect(starts.append)
        self.dialog.stop_requested.connect(lambda: stops.append(True))
        self.dialog.append("axis one\naxis two\naxis three\n")
        self.dialog.text.setFocus()
        QTest.keyClick(self.dialog.text, Qt.Key_F, Qt.ControlModifier)
        self.assertTrue(self.dialog.search.hasFocus())
        QTest.keyClicks(self.dialog.search, "axis")
        # Enter immediately after typing must apply the current query first.
        QTest.keyClick(self.dialog.search, Qt.Key_Return)
        self.assertEqual(self.dialog.match_count.text(), "1 / 3")
        QTest.keyClick(self.dialog.search, Qt.Key_Return)
        self.assertEqual(self.dialog.match_count.text(), "2 / 3")
        QTest.keyClick(self.dialog.search, Qt.Key_Return, Qt.ShiftModifier)
        self.assertEqual(self.dialog.match_count.text(), "1 / 3")
        QTest.keyClick(self.dialog.search, Qt.Key_F3, Qt.ShiftModifier)
        self.assertEqual(self.dialog.match_count.text(), "3 / 3")
        QTest.keyClick(self.dialog.search, Qt.Key_F3)
        self.assertEqual(self.dialog.match_count.text(), "1 / 3")
        QTest.keyClick(self.dialog.search, Qt.Key_Escape)
        self.settle(lambda: not self.dialog.search_timer.isActive())
        self.assertEqual(self.dialog.match_count.text(), "0 / 0")
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(starts, [])
        self.assertEqual(stops, [])

    def test_new_output_updates_count_without_moving_current_match_or_viewport(self):
        self.dialog.append("axis first\n" + "INFO other line\n" * 100 + "axis last\n")
        self.search("axis", "1 / 2")
        self.assertFalse(self.dialog.follow.isChecked())
        position = self.dialog.text.current_match.selectionStart()
        scroll = self.dialog.text.verticalScrollBar().value()
        self.dialog.append("axis arriving\n" + "INFO output\n" * 20)
        self.settle(lambda: self.dialog.match_count.text() == "1 / 3")
        self.assertEqual(self.dialog.text.current_match.selectionStart(), position)
        self.assertEqual(self.dialog.text.verticalScrollBar().value(), scroll)
        self.dialog.latest_button.click()
        self.assertTrue(self.dialog.follow.isChecked())
        self.assertEqual(self.dialog.text.verticalScrollBar().value(), self.dialog.text.verticalScrollBar().maximum())

    def test_partial_match_and_buffer_eviction_recount_and_clear(self):
        self.dialog.text.setMaximumBlockCount(4)
        self.dialog.append("ERROR first\nINFO middle\nINFO third\nER")
        self.search("ERROR", "1 / 1")
        self.dialog.append("ROR latest")
        self.settle(lambda: self.dialog.match_count.text() == "1 / 2")
        self.dialog.append("\nINFO replacement")
        self.settle(lambda: self.dialog.match_count.text() == "0 / 1")
        self.assertNotIn("ERROR first", self.dialog.text.toPlainText())
        self.assertFalse(self.dialog.text.current_match.hasSelection())
        self.dialog.next_button.click()
        self.assertEqual(self.dialog.text.current_match.selectedText(), "ERROR")
        self.assertEqual(self.dialog.match_count.text(), "1 / 1")
        self.dialog.text.clear()
        self.assertEqual(self.dialog.match_count.text(), "0 / 0")
        self.assertFalse(self.dialog.text.extraSelections())

    def test_keyboard_line_jump_and_follow_are_independent_of_receiving(self):
        self.dialog.append("".join(f"INFO line {i}\n" for i in range(1, 101)))
        self.dialog.text.refresh_counts()
        self.dialog.text.setFocus()
        QTest.keyClick(self.dialog.text, Qt.Key_G, Qt.ControlModifier)
        self.assertTrue(self.dialog.line_number.hasFocus())
        self.dialog.line_number.setValue(42)
        QTest.keyClick(self.dialog.line_number, Qt.Key_Return)
        self.assertEqual(self.dialog.text.textCursor().blockNumber(), 41)
        self.assertFalse(self.dialog.follow.isChecked())
        self.dialog.append("INFO still receiving\n")
        self.assertIn("still receiving", self.dialog.text.toPlainText())

    def test_wrapped_unicode_and_unbroken_payload_preserve_raw_copy_and_export(self):
        text = self.dialog.text
        payload = "INFO 中文日志 😀 " + "0123456789ABCDEF" * 80 + " END\nWARN 下一条日志\n"
        self.dialog.append(payload)
        self.app.processEvents()
        self.assertTrue(self.dialog.wrap.isChecked())
        self.assertGreater(text.document().firstBlock().layout().lineCount(), 1)
        self.assertEqual(text.blockCount(), 3)
        self.assertEqual(text.horizontalScrollBar().maximum(), 0)
        self.assertEqual(text.toPlainText(), payload)
        text.selectAll()
        text.copy()
        self.assertEqual(self.app.clipboard().text(), payload)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "log.txt"
            with patch("devmem_studio.dialogs.QFileDialog.getSaveFileName", return_value=(str(target), "")):
                self.dialog.save_log()
            self.assertEqual(target.read_text(encoding="utf-8"), payload)
        self.dialog.wrap.setChecked(False)
        self.app.processEvents()
        self.assertEqual(text.document().firstBlock().layout().lineCount(), 1)
        self.assertGreater(text.horizontalScrollBar().maximum(), 0)
        self.dialog.wrap.setChecked(True)
        self.app.processEvents()
        self.assertGreater(text.document().firstBlock().layout().lineCount(), 1)
        self.assertEqual(text.blockCount(), 3)
        self.assertEqual(text.toPlainText(), payload)

    def test_wrap_and_number_toggles_preserve_search_and_original_line_navigation(self):
        starts, stops = [], []
        self.dialog.start_requested.connect(starts.append)
        self.dialog.stop_requested.connect(lambda: stops.append(True))
        text = self.dialog.text
        self.dialog.append("INFO " + "A17B23CC" * 100 + " TARGET\nWARN second original line\n" + "INFO later\n" * 100)
        self.search("TARGET", "1 / 1")
        position = text.current_match.selectionStart()
        for checkbox in (self.dialog.wrap, self.dialog.show_line_numbers):
            for enabled in (False, True):
                checkbox.setChecked(enabled)
                self.app.processEvents()
                self.assertEqual(text.current_match.selectionStart(), position)
                self.assertEqual(text.current_match.selectedText(), "TARGET")
                self.assertEqual(self.dialog.match_count.text(), "1 / 1")
                self.assertFalse(self.dialog.follow.isChecked())
        self.dialog.focus_line()
        self.dialog.line_number.setValue(2)
        QTest.keyClick(self.dialog.line_number, Qt.Key_Return)
        self.assertEqual(text.textCursor().blockNumber(), 1)
        self.assertEqual(text.textCursor().block().text(), "WARN second original line")
        self.dialog.append("INFO TARGET still receiving\n")
        self.settle(lambda: self.dialog.match_count.text() == "0 / 2")
        self.assertEqual(text.textCursor().blockNumber(), 1)
        self.dialog.latest_button.click()
        for enabled in (False, True):
            self.dialog.wrap.setChecked(enabled)
            self.app.processEvents()
            self.assertTrue(self.dialog.follow.isChecked())
            self.assertEqual(text.verticalScrollBar().value(), text.verticalScrollBar().maximum())
        self.assertEqual(starts, [])
        self.assertEqual(stops, [])

    def test_line_number_margin_tracks_digits_font_resize_and_buffer_eviction(self):
        text = self.dialog.text
        self.assertTrue(self.dialog.show_line_numbers.isChecked())
        self.assertTrue(text.line_numbers.isVisible())
        self.dialog.append("\n".join(f"INFO 原始日志 {i}" for i in range(1, 100)))
        self.app.processEvents()
        width99 = text.line_numbers.width()
        self.dialog.append("\nINFO 原始日志 100")
        self.app.processEvents()
        width100 = text.line_numbers.width()
        self.assertGreater(width100, width99)
        text.setStyleSheet("QPlainTextEdit#boardConsole { font-size: 20px; }")
        self.dialog.resize(900, 480)
        self.app.processEvents()
        self.assertGreater(text.line_numbers.width(), width100)
        self.assertEqual(text.line_numbers.geometry().right() + 1, text.viewport().geometry().left())
        self.assertEqual(text.line_numbers.geometry().top(), text.viewport().geometry().top())
        self.assertEqual(text.line_numbers.height(), text.viewport().height())
        self.assertGreater(text.firstVisibleBlock().blockNumber(), 0)
        width_before_eviction = text.line_numbers.width()
        text.setMaximumBlockCount(10)
        self.app.processEvents()
        self.assertEqual(text.blockCount(), 10)
        self.assertLess(text.line_numbers.width(), width_before_eviction)
        text.jump_to_line(1)
        self.assertEqual(text.textCursor().block().text(), "INFO 原始日志 91")
        self.dialog.show_line_numbers.setChecked(False)
        self.app.processEvents()
        self.assertFalse(text.line_numbers.isVisible())
        self.assertEqual(text.viewportMargins().left(), 0)
        self.dialog.show_line_numbers.setChecked(True)
        text.clear()
        self.app.processEvents()
        self.assertTrue(text.line_numbers.isVisible())
        self.assertEqual(text.viewportMargins().left(), text.line_numbers.width())
        self.assertEqual(text.blockCount(), 1)
        self.assertEqual(self.dialog.line_count.text(), "/ 1 行")


if __name__ == "__main__":
    unittest.main()
