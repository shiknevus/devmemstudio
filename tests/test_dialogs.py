"""Dialog-level regressions: bit upload/rollback must never raise out of a Qt slot
on bad user input (stale clipboard paths, hand-renamed board backups)."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QDialogButtonBox, QLabel, QSizePolicy  # noqa: E402

from devmem_studio.dialogs import BitUploadDialog, BitRollbackDialog, FileChoiceDialog  # noqa: E402


class FileChoiceDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_single_result_is_visible_and_selected(self):
        root = Path(tempfile.gettempdir())
        candidate = root / 'one-top.sv'
        dialog = FileChoiceDialog('top', 'choose', root, [(candidate, ['2'])], ['components'])
        try:
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog.table.item(0, 0).text(), candidate.name)
            self.assertEqual(dialog.selected_path(), str(candidate))
        finally:
            dialog.close()
            dialog.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_multiple_selection_keeps_batch_default_and_disables_empty_acceptance(self):
        root = Path(tempfile.gettempdir())
        candidates = [root / 'component-a', root / 'component-b']
        dialog = FileChoiceDialog('components', 'choose', root, [(path, []) for path in candidates], [],
                                  multiple=True, path_header='component folder')
        try:
            self.assertEqual(dialog.selected_paths(), list(map(str, candidates)))
            accept = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
            dialog.table.clearSelection()
            self.assertEqual(dialog.selected_paths(), [])
            self.assertFalse(accept.isEnabled())
            dialog.table.selectRow(1)
            self.assertEqual(dialog.selected_paths(), [str(candidates[1])])
            self.assertTrue(accept.isEnabled())
        finally:
            dialog.close()
            dialog.deleteLater()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


class BitUploadDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_set_path_accepts_existing_bit(self):
        dialog = BitUploadDialog()
        with tempfile.TemporaryDirectory() as directory:
            bit = Path(directory) / "fw.bit"
            bit.write_bytes(b"\x00" * 16)
            with patch.object(QMessageBox, "warning") as warning:
                dialog.set_path(str(bit))
            warning.assert_not_called()
            self.assertEqual(dialog._local_path, str(bit))
            self.assertIn("16", dialog.status.text())
            self.assertEqual(dialog.path_label.text(), str(bit))

    def test_set_path_rejects_nonexistent_bit_without_raising(self):
        dialog = BitUploadDialog()
        missing = Path(tempfile.gettempdir()) / "no-such-board-upload.bit"
        with patch.object(QMessageBox, "warning") as warning:
            dialog.set_path(str(missing))   # a stale clipboard URL must not raise FileNotFoundError
        warning.assert_called_once()
        self.assertIsNone(dialog._local_path)
        self.assertEqual(dialog.path_label.text(), "未选择文件")

    def test_set_path_rejects_non_bit_suffix(self):
        dialog = BitUploadDialog()
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "fw.bin"
            other.write_bytes(b"x")
            with patch.object(QMessageBox, "warning") as warning:
                dialog.set_path(str(other))
        warning.assert_called_once()
        self.assertIsNone(dialog._local_path)

    def test_long_path_is_elided_without_covering_dialog_content(self):
        dialog = BitUploadDialog()
        with tempfile.TemporaryDirectory() as directory:
            bit = Path(directory) / ("firmware-" + "x" * 170 + ".bit")
            bit.write_bytes(b"x")
            dialog.resize(600, 320)
            dialog.show()
            self.app.processEvents()
            dialog.set_path(str(bit))
            self.app.processEvents()
            self.assertEqual(dialog.path_label.text(), str(bit))
            self.assertEqual(dialog.path_label.toolTip(), str(bit))
            self.assertEqual(dialog.path_label.sizePolicy().horizontalPolicy(), QSizePolicy.Ignored)
            self.assertNotEqual(QLabel.text(dialog.path_label), str(bit))
            heading = dialog.layout().itemAt(0).widget()
            self.assertLessEqual(heading.geometry().bottom(), dialog.path_label.geometry().top())
            self.assertLess(dialog.path_label.geometry().bottom(), dialog.status.geometry().top())
        dialog.close()


class BitRollbackDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_populate_handles_invalid_and_foreign_timestamps(self):
        dialog = BitRollbackDialog()
        entries = [
            {"name": "sunny_fpga.bit", "timestamp": None},
            {"name": "sunny_fpga.bit_20260914120000", "timestamp": "20260914120000"},
            {"name": "sunny_fpga.bit_20261301000000", "timestamp": "20261301000000"},  # 非法月份
            {"name": "sunny_fpga.bit_20260230000000", "timestamp": "20260230000000"},  # 非法日期
            {"name": "sunny_fpga.bit_handwritten", "timestamp": "handwritten"},        # 非 14 位戳
        ]
        dialog.populate(entries, "测试列表")   # must not raise ValueError
        self.assertEqual(dialog.list.count(), 5)
        texts = [dialog.list.item(i).text() for i in range(dialog.list.count())]
        self.assertTrue(any("2026-09-14 12:00:00" in text for text in texts), "合法戳按日期格式化")
        self.assertTrue(any("20261301000000" in text for text in texts), "非法月份回退原始戳")
        self.assertTrue(any("20260230000000" in text for text in texts), "非法日期回退原始戳")
        self.assertTrue(any("handwritten" in text for text in texts), "非数字戳原样显示")
        self.assertEqual(dialog.status.text(), "测试列表")
        self.assertFalse(dialog._busy)

    def test_populate_current_bit_is_download_only(self):
        dialog = BitRollbackDialog()
        dialog.populate([{"name": "sunny_fpga.bit", "timestamp": None},
                         {"name": "sunny_fpga.bit_20260914120000", "timestamp": "20260914120000"}], "")
        self.assertEqual(dialog.selected_backup(), "sunny_fpga.bit_20260914120000")   # newest backup preselected
        self.assertTrue(dialog.rollback_button.isEnabled())
        live = dialog.list.item(0)
        self.assertTrue(live.flags() & Qt.ItemIsEnabled and live.flags() & Qt.ItemIsSelectable, "当前版本可选中")
        dialog.list.setCurrentRow(0)
        self.assertEqual(dialog.selected_backup(), "sunny_fpga.bit")
        self.assertTrue(dialog.download_button.isEnabled())
        self.assertFalse(dialog.rollback_button.isEnabled(), "当前版本不可作为回退目标")
        rollbacks, downloads = [], []
        dialog.rollback_requested.connect(rollbacks.append)
        dialog.download_requested.connect(downloads.append)
        dialog.list.itemDoubleClicked.emit(live)   # double-click means rollback: ignored for the live bit
        dialog.start_rollback()
        self.assertEqual(rollbacks, [])
        dialog.download_button.click()
        self.assertEqual(downloads, ["sunny_fpga.bit"])
        dialog.list.setCurrentRow(1)
        self.assertTrue(dialog.rollback_button.isEnabled())

    def test_populate_only_current_bit_selects_it_for_download(self):
        dialog = BitRollbackDialog()
        dialog.populate([{"name": "sunny_fpga.bit", "timestamp": None}], "")
        self.assertEqual(dialog.selected_backup(), "sunny_fpga.bit")
        self.assertTrue(dialog.download_button.isEnabled())
        self.assertFalse(dialog.rollback_button.isEnabled())

    def test_download_button_follows_selection_and_busy(self):
        dialog = BitRollbackDialog()
        self.assertFalse(dialog.download_button.isEnabled())
        dialog.populate([{"name": "sunny_fpga.bit", "timestamp": None},
                         {"name": "sunny_fpga.bit_20260914120000", "timestamp": "20260914120000"}], "")
        self.assertTrue(dialog.download_button.isEnabled())
        requested = []
        dialog.download_requested.connect(requested.append)
        dialog.download_button.click()
        self.assertEqual(requested, ["sunny_fpga.bit_20260914120000"])
        dialog.set_busy(True)
        self.assertFalse(dialog.download_button.isEnabled())
        dialog.start_download()   # busy: ignored
        self.assertEqual(len(requested), 1)
        dialog.populate([], "")
        self.assertFalse(dialog.download_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
