"""Styled dialog, cooperative engine jobs, compact toolbar and elision checks."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QScrollArea
from devmem_studio.bitpack_dialog import BitPackDialog
from devmem_studio.core import ConfigStore
from devmem_studio.theme import STYLE, icon
from devmem_studio.widgets import ElidedLabel
from devmem_studio.window import MainWindow


class StyledPackDialogTests(unittest.TestCase):
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
        self.root = Path(self.temp.name)
        self.cache_patch = patch("devmem_studio.bitpack.user_data_dir", return_value=self.root / "cache")
        self.cache_patch.start()
        self.dialog = BitPackDialog(directory=self.root)
        self.dialog.show()
        self.app.processEvents()
        self.source = self.root / "源文件 空格.bit"
        self.source.write_bytes(bytes(range(256)) * 8)

    def settle(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            if predicate():
                return
            time.sleep(0.01)
        self.fail("GUI/engine did not settle")

    def tearDown(self):
        if self.dialog.busy:
            self.dialog.cancel_task()
            self.settle(lambda: not self.dialog.busy)
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.cache_patch.stop()
        self.temp.cleanup()

    def configure(self):
        self.dialog.set_source(self.source)
        self.dialog.project.setText("A100测试")

    def test_folder_selection_scans_in_background_and_selects_from_multiple_files(self):
        nested = self.root / "FPGA folder" / "子目录"
        nested.mkdir(parents=True)
        first = nested.parent / "a.bit"
        second = nested / "b.bit"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        self.dialog.scan_directory(nested.parent)
        self.assertTrue(self.dialog.busy)
        self.settle(lambda: not self.dialog.busy)
        self.assertEqual(self.dialog.source_combo.count(), 2)
        self.assertEqual(self.dialog._source_directory, str(nested.parent))
        self.dialog.source_combo.setCurrentIndex(1)
        self.assertEqual(self.dialog.source.text(), str(second))
        self.assertEqual(self.dialog.preference_snapshot()["source_directory"], str(nested.parent))
        self.assertEqual(self.dialog.preference_snapshot()["source_mode"], "folder")
        self.dialog.project.setText("FOLDER")
        self.dialog.start_pack()
        self.settle(lambda: not self.dialog.busy)
        with zipfile.ZipFile(self.dialog._result_path) as package:
            self.assertEqual(package.read("sunny_fpga.bit"), b"second")

    def test_empty_folder_keeps_selected_directory_and_reports_no_bit(self):
        folder = self.root / "空目录"
        folder.mkdir()
        self.dialog.scan_directory(folder)
        self.settle(lambda: not self.dialog.busy)
        self.assertEqual(self.dialog.source_combo.count(), 0)
        self.assertEqual(self.dialog.source.text(), "")
        self.assertIn("未找到", self.dialog.status.text())
        self.assertEqual(self.dialog.preference_snapshot()["source_directory"], str(folder))

    def test_restored_project_and_directories_override_fallback_without_losing_output(self):
        selected = self.root / "上次选择"
        output = self.root / "上次输出"
        selected.mkdir()
        output.mkdir()
        source = selected / "saved.bit"
        source.write_bytes(b"saved")
        saved = {"project": "A200", "source_directory": str(selected),
                 "output_directory": str(output), "source_path": str(source), "source_mode": "file"}
        restored = BitPackDialog(directory=self.root, settings=saved)
        try:
            self.assertEqual(restored.project.text(), "A200")
            self.assertEqual(restored.source.text(), str(source))
            self.assertEqual(restored._browse_directory, str(selected))
            self.assertEqual(restored.output.text(), str(output))
            restored.set_source(self.source)
            self.assertEqual(restored.output.text(), str(output))
        finally:
            restored.close()
            restored.deleteLater()

    def test_last_folder_is_rescanned_on_new_dialog_and_previous_choice_is_restored(self):
        folder = self.root / "扫描恢复"
        folder.mkdir()
        first, second = folder / "a.bit", folder / "b.bit"
        first.write_bytes(b"a")
        second.write_bytes(b"b")
        restored = BitPackDialog(directory=self.root, settings={"project": "P1",
            "source_directory": str(folder), "source_mode": "folder", "source_path": str(second),
            "output_directory": str(self.root)})
        try:
            restored.show()
            self.app.processEvents()
            self.settle(lambda: not restored.busy and restored.source_combo.count() == 2)
            self.assertEqual(restored.source.text(), str(second))
            self.assertEqual(restored.project.text(), "P1")
            self.assertEqual(restored.output.text(), str(self.root))
        finally:
            if restored.busy:
                restored.cancel_task()
                self.settle(lambda: not restored.busy)
            restored.close()
            restored.deleteLater()

    def test_dialog_uses_shared_style_and_primary_button(self):
        self.assertEqual(self.dialog.styleSheet(), "")  # inherits the app theme, no separate palette
        self.assertEqual(self.app.styleSheet(), STYLE)
        self.assertEqual(self.dialog.start_button.objectName(), "primary")
        self.assertEqual(self.dialog.details.objectName(), "console")
        self.assertEqual(self.dialog.timeout.value(), 300)

    def test_source_and_output_show_complete_paths_in_tooltips(self):
        self.configure()
        self.assertEqual(self.dialog.source.toolTip(), str(self.source))
        self.assertEqual(self.dialog.output.toolTip(), str(self.root))

    def test_custom_output_directory_is_not_reset_by_new_source(self):
        output = self.root / "输出"
        output.mkdir()
        self.dialog.output.setText(str(output))
        self.dialog._output_edited(str(output))
        self.dialog.set_source(self.source)
        self.assertEqual(self.dialog.output.text(), str(output))

    def test_nopack_disables_project_but_keeps_it_for_next_zip(self):
        self.dialog.project.setText("A100")
        self.dialog.nopack.setChecked(True)
        self.assertFalse(self.dialog.project.isEnabled())
        self.dialog.nopack.setChecked(False)
        self.assertTrue(self.dialog.project.isEnabled())
        self.assertEqual(self.dialog.project.text(), "A100")

    def test_invalid_project_does_not_start_engine(self):
        self.configure()
        self.dialog.project.setText("invalid project")
        with patch("devmem_studio.bitpack_dialog.QMessageBox.warning") as warning:
            self.dialog.start_pack()
        warning.assert_called_once()
        self.assertFalse(self.dialog.busy)
        self.assertIsNone(self.dialog._worker)

    def test_empty_output_does_not_silently_write_to_current_directory(self):
        self.configure()
        self.dialog.output.clear()
        with patch("devmem_studio.bitpack_dialog.QMessageBox.warning") as warning:
            self.dialog.start_pack()
        warning.assert_called_once()
        self.assertFalse(self.dialog.busy)

    def test_real_job_runs_off_thread_and_publishes_verified_zip(self):
        self.configure()
        self.dialog.start_pack()
        self.assertTrue(self.dialog.busy)
        self.assertFalse(self.dialog.start_button.isEnabled())
        self.assertFalse(self.dialog.source.isEnabled())
        self.settle(lambda: not self.dialog.busy)
        self.assertEqual(self.dialog.status.property("state"), "connected")
        self.assertEqual(self.dialog.progress.value(), 100)
        self.assertTrue(self.dialog.open_output_button.isEnabled())
        with zipfile.ZipFile(self.dialog._result_path) as package:
            self.assertEqual(package.namelist(), ["sunny_fpga.bit"])
            self.assertEqual(package.read("sunny_fpga.bit"), self.source.read_bytes())
        self.assertIsNone(self.dialog._cancel)

    def test_copy_overwrite_requires_confirmation_and_cancel_preserves_old_copy(self):
        self.configure()
        target = self.root / "sunny_fpga.bit"
        target.write_bytes(b"old copy")
        self.dialog.nopack.setChecked(True)
        with patch("devmem_studio.bitpack_dialog.QMessageBox.question", return_value=QMessageBox.No):
            self.dialog.start_pack()
        self.assertFalse(self.dialog.busy)
        self.assertEqual(target.read_bytes(), b"old copy")

    def test_copy_overwrite_yes_uses_safe_engine_and_preserves_source(self):
        self.configure()
        target = self.root / "sunny_fpga.bit"
        target.write_bytes(b"old copy")
        self.dialog.nopack.setChecked(True)
        with patch("devmem_studio.bitpack_dialog.QMessageBox.question", return_value=QMessageBox.Yes):
            self.dialog.start_pack()
        self.settle(lambda: not self.dialog.busy)
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.dialog._result_path, target)

    def test_close_while_packing_requests_safe_cancel_and_waits_for_cleanup(self):
        with self.source.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024)
        self.configure()
        self.dialog.start_pack()
        self.dialog.close()
        self.assertTrue(self.dialog.busy)
        self.settle(lambda: not self.dialog.busy and not self.dialog.isVisible())
        self.assertIn("已取消", self.dialog.status.text())
        self.assertEqual(self.source.stat().st_size, 64 * 1024 * 1024)
        self.assertEqual(list(self.root.glob("A100测试_bit_*.zip")), [])
        self.assertEqual(list(self.root.glob(".bitpack-*.tmp")), [])


class CompactWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        StyledPackDialogTests.setUpClass()
        cls.app = StyledPackDialogTests.app

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.window = MainWindow(ConfigStore(self.root / "settings.json"), persist=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        if self.window.bit_pack_dialog:
            self.window.bit_pack_dialog.close()
            self.window.bit_pack_dialog.deleteLater()
        self.window.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.temp.cleanup()

    def test_project_directories_and_nopack_persist_across_software_instances(self):
        source_dir, output_dir = self.root / "输入", self.root / "输出"
        source_dir.mkdir()
        output_dir.mkdir()
        source = source_dir / "remember.bit"
        source.write_bytes(b"source")
        self.window.persist = True  # config path remains this test's temporary directory
        self.window.bit_pack_button.click()
        dialog = self.window.bit_pack_dialog
        dialog.set_source(source)
        dialog.project.setText("REMEMBER测试")
        dialog.output.setText(str(output_dir))
        dialog._output_edited(str(output_dir))
        for nopack in (True, False):
            with self.subTest(nopack=nopack):
                dialog.nopack.setChecked(nopack)
                self.assertEqual(self.window.cfg["bitpack_settings"]["nopack"], nopack)
                self.assertTrue(self.window.save_timer.isActive())
                self.window.save_settings()
                saved = self.window.store.load()["bitpack_settings"]
                self.assertEqual(saved["project"], "REMEMBER测试")
                self.assertEqual(saved["source_directory"], str(source_dir))
                self.assertEqual(saved["output_directory"], str(output_dir))
                self.assertEqual(saved["nopack"], nopack)
                second = MainWindow(ConfigStore(self.window.store.path), persist=False)
                try:
                    second.open_bit_pack()
                    self.assertEqual(second.bit_pack_dialog.project.text(), "REMEMBER测试")
                    self.assertEqual(second.bit_pack_dialog.source.text(), str(source))
                    self.assertEqual(second.bit_pack_dialog.output.text(), str(output_dir))
                    self.assertEqual(second.bit_pack_dialog._browse_directory, str(source_dir))
                    self.assertEqual(second.bit_pack_dialog.nopack.isChecked(), nopack)
                    self.assertEqual(second.bit_pack_dialog.project.isEnabled(), not nopack)
                finally:
                    second.bit_pack_dialog.close()
                    second.bit_pack_dialog.deleteLater()
                    second.close()
                    second.deleteLater()

    def test_compact_sidebar_is_fixed_tree_only_and_has_no_outer_scroll_area(self):
        self.window.resize(1280, 800)
        for _ in range(8):
            self.app.processEvents()
        self.assertEqual(self.window.sidebar_scroll.width(), 300)
        self.assertEqual(self.window.side_split.sizes()[0], 300)
        self.assertEqual(self.window.workspace.x(), 300 + self.window.side_split.handleWidth())
        self.assertFalse(self.window.side_split.handle(1).isEnabled())
        self.assertFalse(self.window.serial_section.is_expanded())
        self.assertTrue(self.window.connect_button.isVisible())
        self.assertTrue(self.window.serial_connect_button.isVisible())
        self.assertTrue(self.window.component_tree.isVisible())
        self.assertNotIsInstance(self.window.sidebar_scroll, QScrollArea)
        self.assertEqual(self.window.sidebar_scroll.findChildren(QScrollArea), [])
        self.assertTrue(self.window.sidebar_content.rect().contains(self.window.component_tree.geometry()))
        self.window.side_split.setSizes([450, 800])
        self.app.processEvents()
        self.assertEqual(self.window.sidebar_scroll.width(), 300)

    def test_compact_channel_opens_inline_one_at_a_time_without_hiding_the_tree(self):
        self.window.resize(1280, 800)
        for _ in range(8):
            self.app.processEvents()
        self.window.ssh_section.set_expanded(False)
        self.window.ssh_section.toggle.click()
        self.app.processEvents()
        self.assertTrue(self.window.ssh_section.is_expanded())
        self.assertTrue(self.window.user.isVisible())
        self.window.user.setText("remembered-user")
        self.window.serial_section.toggle.click()
        for _ in range(4):
            self.app.processEvents()
        self.assertTrue(self.window.serial_port.isVisible())
        self.assertFalse(self.window.ssh_section.is_expanded())
        self.assertFalse(self.window.user.isVisible())
        self.assertTrue(self.window.component_tree.isVisible())
        self.assertTrue(self.window.sidebar_content.rect().contains(self.window.component_tree.geometry()))
        self.assertEqual(self.window.user.text(), "remembered-user")
        self.assertEqual(self.window.sidebar_scroll.findChildren(QScrollArea), [])

    def test_expanding_main_window_keeps_channel_state_and_restores_resizable_rail(self):
        self.window.resize(1280, 800)
        for _ in range(8):
            self.app.processEvents()
        self.window.serial_section.toggle.click()
        self.window.resize(1540, 960)
        for _ in range(8):
            self.app.processEvents()
        self.assertTrue(self.window.serial_section.is_expanded())
        self.assertTrue(self.window.serial_port.isVisible())
        self.assertTrue(self.window.side_split.handle(1).isEnabled())
        self.assertGreaterEqual(self.window.sidebar_scroll.width(), 320)

    def test_offline_button_opens_and_reuses_styled_dialog(self):
        self.assertTrue(self.window.bit_pack_button.isEnabled())
        self.window.bit_pack_button.click()
        first = self.window.bit_pack_dialog
        self.assertTrue(first.isVisible())
        self.assertIs(first.parentWidget(), self.window)
        self.window.bit_pack_button.click()
        self.assertIs(self.window.bit_pack_dialog, first)
        first.close()
        self.window.bit_pack_button.click()
        self.assertTrue(first.isVisible())

    def test_packing_does_not_depend_on_register_task_gate(self):
        self.window._busy = True
        try:
            self.window._refresh_controls()
            self.window.bit_pack_button.click()
            self.assertTrue(self.window.bit_pack_dialog.isVisible())
        finally:
            self.window._busy = False

    def test_closing_workbench_cancels_engine_and_waits_for_clean_exit(self):
        source = self.root / "关闭测试.bit"
        with source.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024)
        self.window.bit_pack_button.click()
        dialog = self.window.bit_pack_dialog
        dialog.set_source(source)
        dialog.project.setText("CLOSE")
        with patch("devmem_studio.bitpack.user_data_dir", return_value=self.root / "cache"):
            dialog.start_pack()
            self.window.close()
            self.assertTrue(dialog.busy)
            deadline = time.monotonic() + 10
            while (dialog.busy or self.window.isVisible()) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertFalse(dialog.busy)
        self.assertFalse(self.window.isVisible())
        self.assertIn("已取消", dialog.status.text())
        self.assertEqual(source.stat().st_size, 64 * 1024 * 1024)
        self.assertEqual(list(self.root.glob("CLOSE_bit_*.zip")), [])

    def test_search_and_all_firmware_buttons_share_a_row_and_fit_compact_width(self):
        for width in (960, 1280, 1540, 1920):
            self.window.resize(width, 800 if width <= 1280 else 960)
            for _ in range(8):
                self.app.processEvents()
            center = self.window.log_search.geometry().center().y()
            for control in (self.window.bit_pack_button, self.window.bit_upload_button,
                            self.window.bit_rollback_button, self.window.log_download_button,
                            self.window.reboot_button, self.window.log_export_button,
                            self.window.log_clear_button):
                self.assertTrue(control.isVisible(), (width, control.text()))
                self.assertLessEqual(abs(control.geometry().center().y() - center), 2)
                self.assertTrue(control.parentWidget().rect().contains(control.geometry()), (width, control.geometry()))
                self.assertGreaterEqual(control.width(), control.minimumSizeHint().width())

    def test_compact_selectors_are_short_but_retain_data_and_complete_tooltips(self):
        self.window.resize(1280, 800)
        for _ in range(8):
            self.app.processEvents()
        self.assertEqual(self.window.log_filter.itemText(0), "全部")
        self.assertEqual(self.window.log_filter.itemData(0, Qt.ToolTipRole), "全部日志")
        self.assertEqual(self.window.console_source_combo.itemText(0), "log")
        self.assertEqual(self.window.console_source_combo.itemData(0), "log")
        self.assertEqual(self.window.console_source_combo.itemData(0, Qt.ToolTipRole), "tail log")
        self.window.resize(1920, 960)
        for _ in range(8):
            self.app.processEvents()
        self.assertEqual(self.window.log_filter.itemText(0), "全部日志")
        self.assertEqual(self.window.log_download_button.text(), "下载log")

    def test_import_upload_download_icons_have_distinct_pixels(self):
        size = self.window.log_download_button.iconSize()
        images = [control.icon().pixmap(size).toImage() for control in
                  (self.window.import_button, self.window.bit_upload_button, self.window.log_download_button)]
        self.assertNotEqual(images[0], images[1])
        self.assertNotEqual(images[1], images[2])
        self.assertNotEqual(images[0], images[2])

    def test_elided_labels_keep_full_text_semantics_and_tooltips(self):
        text = "A0001_很长的设备名字与组件类型说明"
        widget = ElidedLabel(text, "sectionTitle")
        widget.resize(90, 32)
        widget.show()
        self.app.processEvents()
        self.assertEqual(widget.text(), text)
        self.assertEqual(widget.toolTip(), text)
        self.assertNotEqual(QLabel.text(widget), text)
        self.assertIn("…", QLabel.text(widget))
        widget.resize(800, 32)
        self.app.processEvents()
        self.assertEqual(QLabel.text(widget), text)
        widget.close()
        widget.deleteLater()


if __name__ == "__main__":
    unittest.main()
