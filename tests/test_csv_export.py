"""Lossless export, filename selection and safe failure behavior."""
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from devmem_studio.csv_export import CSV_FILTER, ZIP_FILTER, csv_export, export_path


class CsvExportTests(unittest.TestCase):
    def test_zip_restores_identical_csv_bytes_with_unicode_quotes_and_empty_values(self):
        rows = [["寄存器", "备注", "数值"], ["位置😀", '逗号,引号"换行\n', -2147483648],
                ["读取失败", "", ""], ["最大值", "", 4294967295]]
        with tempfile.TemporaryDirectory() as folder:
            plain = Path(folder) / "数据.csv"
            packed = Path(folder) / "数据.ZIP"
            for path in (plain, packed):
                with csv_export(path) as handle:
                    csv.writer(handle).writerows(rows)
            with ZipFile(packed) as archive:
                self.assertEqual(archive.namelist(), ["数据.csv"])
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.read("数据.csv"), plain.read_bytes())
            self.assertTrue(plain.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_filename_uses_explicit_extension_or_selected_format(self):
        for name, selected, expected in (("data", ZIP_FILTER, "data.zip"),
                                          ("data", CSV_FILTER, "data.csv"),
                                          ("data.part", ZIP_FILTER, "data.part.zip"),
                                          ("data.CSV", ZIP_FILTER, "data.CSV"),
                                          ("data.ZIP", CSV_FILTER, "data.ZIP")):
            with self.subTest(name=name, selected=selected):
                self.assertEqual(export_path(name, selected), expected)

    def test_failed_export_keeps_existing_file_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            for extension in ("csv", "zip"):
                with self.subTest(extension=extension):
                    target = Path(folder) / f"data.{extension}"
                    target.write_bytes(b"previous export")
                    with self.assertRaises(OSError):
                        with csv_export(target) as handle:
                            handle.write("incomplete")
                            raise OSError("disk full")
                    self.assertEqual(target.read_bytes(), b"previous export")
                    self.assertEqual(list(Path(folder).glob(".csv-export-*")), [])
                    with patch("devmem_studio.csv_export.os.replace", side_effect=PermissionError("locked")):
                        with self.assertRaises(PermissionError):
                            with csv_export(target) as handle:
                                handle.write("complete")
                    self.assertEqual(target.read_bytes(), b"previous export")
                    self.assertEqual(list(Path(folder).glob(".csv-export-*")), [])
