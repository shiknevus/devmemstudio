"""Fast checks stay deterministic and report environment drift clearly."""
from pathlib import Path
import tempfile
import unittest

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_project  # noqa: E402


class ProjectCheckTests(unittest.TestCase):
    def test_locked_packages_parses_pins_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lock.txt"
            path.write_text("A-Pkg==1.2\n# ignored\nB_pkg==3.4 ; python_version >= '3.12'\n", encoding="utf-8")
            self.assertEqual(check_project.locked_packages(path), {"a_pkg": "1.2", "b_pkg": "3.4"})

    def test_catalog_checks_warn_about_machine_specific_source_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text('{"types": {"ec_uut": {}}, "source": {"rtl_root": "E:\\\\rtl"}}',
                            encoding="utf-8")
            self.assertEqual(check_project.catalog_issues(path), ["catalog embeds absolute RTL path: E:\\rtl"])

    def test_catalog_checks_empty_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.json"
            path.write_text('{"types": {}}', encoding="utf-8")
            self.assertEqual(check_project.catalog_issues(path), ["catalog has no types"])


if __name__ == "__main__":
    unittest.main()
