"""Bundled tool extraction, launch lifecycle and safe failure regressions."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from devmem_studio import bitpack
from devmem_studio.core import resource_path


class BitPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "工具 空格" / "pack_bit.exe"
        self.source.parent.mkdir()
        self.payload = b"MZ test bundled native packer"
        self.source.write_bytes(self.payload)
        self.cache_root = self.root / "用户缓存 空格"
        self.resource_patch = patch("devmem_studio.bitpack.resource_path", return_value=self.source)
        self.data_patch = patch("devmem_studio.bitpack.user_data_dir", return_value=self.cache_root)
        self.resource_patch.start()
        self.data_patch.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.resource_patch.stop)
        self.addCleanup(self.data_patch.stop)

    def test_bundled_binary_and_instructions_are_present(self):
        binary = resource_path(bitpack.BITPACK_RESOURCE)
        self.assertTrue(binary.is_file())
        self.assertEqual(binary.read_bytes()[:2], b"MZ")
        self.assertTrue(binary.with_name("使用说明.txt").is_file())

    def test_cache_is_content_addressed_and_independent_of_extraction(self):
        executable = bitpack.prepare_bitpack_executable()
        self.assertEqual(executable.parent.name, hashlib.sha256(self.payload).hexdigest())
        self.assertTrue(executable.is_relative_to(self.cache_root))
        self.assertEqual(executable.read_bytes(), self.payload)
        self.source.unlink()
        self.assertEqual(executable.read_bytes(), self.payload)
        self.assertEqual(list(executable.parent.glob("*.tmp")), [])

    def test_valid_cache_is_reused_without_rewriting_a_running_executable(self):
        first = bitpack.prepare_bitpack_executable()
        with patch("devmem_studio.bitpack.tempfile.NamedTemporaryFile") as temporary:
            self.assertEqual(bitpack.prepare_bitpack_executable(), first)
        temporary.assert_not_called()

    def test_damaged_cache_is_repaired(self):
        first = bitpack.prepare_bitpack_executable()
        first.write_bytes(b"damaged")
        self.assertEqual(bitpack.prepare_bitpack_executable(), first)
        self.assertEqual(first.read_bytes(), self.payload)

    def test_updated_bundle_uses_a_new_cache_without_overwriting_old_version(self):
        first = bitpack.prepare_bitpack_executable()
        self.source.write_bytes(b"MZ new packer")
        second = bitpack.prepare_bitpack_executable()
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), self.payload)
        self.assertEqual(second.read_bytes(), b"MZ new packer")

    def test_missing_bundle_has_a_clear_error_and_does_not_create_a_cache(self):
        self.source.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "内置打包工具缺失"):
            bitpack.prepare_bitpack_executable()
        self.assertFalse(self.cache_root.exists())

    def test_failed_publish_preserves_previous_cache_and_cleans_temporary(self):
        first = bitpack.prepare_bitpack_executable()
        first.write_bytes(b"damaged")
        with patch.object(Path, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                bitpack.prepare_bitpack_executable()
        self.assertEqual(first.read_bytes(), b"damaged")
        self.assertEqual(list(first.parent.glob("*.tmp")), [])
        self.assertEqual(self.source.read_bytes(), self.payload)

    def test_cli_streams_progress_and_packs_without_a_native_window(self):
        import zipfile
        from devmem_studio.bitpack import CancelEvent, run_bitpack
        source = self.root / "源 文件.bit"
        source.write_bytes(bytes(range(256)) * 8)
        # The extraction fixtures use fake bytes; exercise the real bundled engine instead.
        with patch("devmem_studio.bitpack.resource_path", side_effect=resource_path):
            event = CancelEvent()
            try:
                progress = []
                result = run_bitpack(source, self.root, "项目 测试".replace(" ", ""),
                                     False, False, 30, event, progress.append)
            finally:
                event.close()
        self.assertEqual(result["code"], 0)
        self.assertTrue(any(line.startswith("正在") for line in progress))
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertEqual(archive.namelist(), ["sunny_fpga.bit"])
            self.assertEqual(archive.read("sunny_fpga.bit"), source.read_bytes())

    def test_pre_cancelled_job_exits_cleanly_and_preserves_source(self):
        source = self.root / "取消.bit"
        payload = b"original source"
        source.write_bytes(payload)
        with patch("devmem_studio.bitpack.resource_path", side_effect=resource_path):
            event = bitpack.CancelEvent()
            event.cancel()
            try:
                result = bitpack.run_bitpack(source, self.root, "A100", False, False, 30, event, lambda _: None)
            finally:
                event.close()
        self.assertEqual(result["code"], 3)
        self.assertEqual(source.read_bytes(), payload)
        self.assertEqual(list(self.root.glob("A100_bit_*.zip")), [])
        self.assertEqual(list(self.root.glob(".bitpack-*.tmp")), [])

    def test_folder_scan_finds_nested_and_uppercase_bits_but_not_other_files(self):
        folder = self.root / "目录 空格"
        nested = folder / "子目录"
        nested.mkdir(parents=True)
        first, second = folder / "a.bit", nested / "b.BIT"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        (folder / "not-bit.txt").write_bytes(b"ignored")
        (folder / "directory.bit").mkdir()
        with patch("devmem_studio.bitpack.resource_path", side_effect=resource_path):
            event = bitpack.CancelEvent()
            try:
                result = bitpack.scan_bitfiles(folder, event, lambda _: None)
            finally:
                event.close()
        self.assertEqual(result["code"], 0)
        self.assertEqual(set(result["files"]), {str(first), str(second)})
        self.assertFalse(result["truncated"])
        self.assertEqual(result["skipped"], 0)

    def test_pre_cancelled_folder_scan_reports_cancellation(self):
        folder = self.root / "扫描"
        folder.mkdir()
        (folder / "a.bit").write_bytes(b"source")
        with patch("devmem_studio.bitpack.resource_path", side_effect=resource_path):
            event = bitpack.CancelEvent()
            event.cancel()
            try:
                result = bitpack.scan_bitfiles(folder, event, lambda _: None)
            finally:
                event.close()
        self.assertEqual(result["code"], 3)
        self.assertEqual(result["files"], [])
        self.assertEqual((folder / "a.bit").read_bytes(), b"source")

    def test_malformed_saved_preferences_restore_safe_defaults(self):
        self.assertEqual(bitpack.normalize_preferences("broken")["project"], "")
        value = bitpack.normalize_preferences({"project": 123, "source_directory": [],
            "output_directory": {"bad": True}, "source_path": "x" * 33000, "source_mode": "unknown"})
        self.assertEqual(value, {"project": "", "source_directory": "", "output_directory": "",
                                 "source_path": "", "source_mode": "file", "nopack": False})
        for flag in (True, False, "false", "true", 1, None):
            with self.subTest(nopack=flag):
                self.assertEqual(bitpack.normalize_preferences({"nopack": flag})["nopack"], flag is True)

    def test_cancel_events_are_unique_and_idempotently_closed(self):
        first, second = bitpack.CancelEvent(), bitpack.CancelEvent()
        try:
            self.assertNotEqual(first.name, second.name)
            first.cancel()
        finally:
            first.close()
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
