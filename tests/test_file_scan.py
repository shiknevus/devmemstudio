"""Folder scans for .bit / top files and the remembered board directory."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from devmem_studio import top_import
from devmem_studio.core import ConfigStore, DEFAULT_BIT_DIR, normalize_remote_dir, remember_dir
from devmem_studio.file_scan import find_files

TOP = """// --- flow_comp_1 --A0001_axis---
ec_slv_pul_axis
#(
     .REG_SPACE_BIAS     ( 20'd25600)
)
ec_slv_pul_axis_1
(
);
"""


class FileScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def touch(self, relative, mtime, data=b"x"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))
        return path

    def test_newest_first_skips_hidden_and_other_suffixes(self):
        old = self.touch("a/old.bit", 1000)
        new = self.touch("b/c/new.BIT", 2000)
        self.touch(".Xil/cache.bit", 3000)
        self.touch("notes.txt", 4000)
        entries, complete = find_files(self.root, (".bit",))
        self.assertTrue(complete)
        self.assertEqual([path for path, _, _ in entries], [new, old])
        self.assertEqual(entries[0][2], 1)

    def test_depth_and_result_limits_are_reported(self):
        self.touch("1/2/3/deep.bit", 1)
        entries, complete = find_files(self.root, (".bit",), max_depth=2)
        self.assertEqual(entries, [])
        self.assertFalse(complete)
        for index in range(3):
            self.touch(f"many{index}.bit", index)
        entries, complete = find_files(self.root, (".bit",), limit=2)
        self.assertEqual(len(entries), 2)
        self.assertFalse(complete)

    def test_find_top_files_ranks_by_control_count(self):
        two = self.root / "rtl" / "mix_top.sv"
        two.parent.mkdir(parents=True)
        two.write_text(TOP + TOP.replace("flow_comp_1", "flow_comp_2").replace("_1\n", "_2\n")
                       .replace("25600", "26624"), encoding="utf-8")
        one = self.root / "sim" / "tb_top.v"
        one.parent.mkdir(parents=True)
        one.write_text(TOP, encoding="utf-8")
        (self.root / "rtl" / "ec_slv_pul_axis.v").write_text("module ec_slv_pul_axis; endmodule\n", encoding="utf-8")
        candidates, complete = top_import.find_top_files(self.root)
        self.assertTrue(complete)
        self.assertEqual([(item["path"], item["components"]) for item in candidates], [(two, 2), (one, 1)])


class RemoteDirTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_remote_dir(" /run/media/sda/ "), "/run/media/sda")
        self.assertEqual(normalize_remote_dir("//run//media/./usb"), "/run/media/usb")
        self.assertEqual(normalize_remote_dir("\\run\\media\\sdb"), "/run/media/sdb")
        self.assertEqual(normalize_remote_dir("/"), "/")
        for bad in ("", "run/media", "/run/\nmedia", None):
            with self.assertRaises(ValueError):
                normalize_remote_dir(bad)

    def test_remember_dir_moves_to_front_and_caps(self):
        history = [f"/d{index}" for index in range(10)]
        self.assertEqual(remember_dir(history, "/d5")[:2], ["/d5", "/d0"])
        self.assertEqual(len(remember_dir(history, "/new")), 10)
        self.assertEqual(remember_dir(["/a", 3, ""], "/b"), ["/b", "/a"])

    def test_config_round_trip_and_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registers.json"
            path.write_text(json.dumps({"bit_remote_dir": "/run/media/usb1/",
                                        "bit_remote_dirs": ["/run/media/sda", "bad", 7]}), encoding="utf-8")
            cfg = ConfigStore(path).load()
            self.assertEqual(cfg["bit_remote_dir"], "/run/media/usb1")
            self.assertEqual(cfg["bit_remote_dirs"], ["/run/media/usb1", "/run/media/sda"])
            path.write_text(json.dumps({"bit_remote_dir": "relative", "bit_remote_dirs": "nope"}), encoding="utf-8")
            cfg = ConfigStore(path).load()
            self.assertEqual(cfg["bit_remote_dir"], DEFAULT_BIT_DIR)
            self.assertEqual(cfg["bit_remote_dirs"], [DEFAULT_BIT_DIR])


if __name__ == "__main__":
    unittest.main()
