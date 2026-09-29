"""Dialog-level regressions: bit upload/rollback must never raise out of a Qt slot
on bad user input (stale clipboard paths, hand-renamed board backups)."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from devmem_studio.dialogs import BitUploadDialog, BitRollbackDialog  # noqa: E402


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

    def test_populate_current_bit_is_not_a_rollback_target(self):
        dialog = BitRollbackDialog()
        dialog.populate([{"name": "sunny_fpga.bit", "timestamp": None},
                         {"name": "sunny_fpga.bit_20260914120000", "timestamp": "20260914120000"}], "")
        live = dialog.list.item(0)
        self.assertFalse(live.flags() & Qt.ItemIsEnabled, "当前版本不可作为回退目标")
        self.assertEqual(dialog.selected_backup(), "sunny_fpga.bit_20260914120000")


if __name__ == "__main__":
    unittest.main()
