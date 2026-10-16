"""Workspace cleanup is preview-first and never reaches outside its root."""
from pathlib import Path
import os
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import cleanup_workspace  # noqa: E402


class CleanupWorkspaceTests(unittest.TestCase):
    def test_release_plan_keeps_newest_versions_and_sidecars_together(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dist = root / "dist"
            dist.mkdir()
            for version in ("5.3.0", "5.4.0", "5.5.1", "5.5.2"):
                (dist / f"DevmemStudio-{version}-win64.zip").write_bytes(version.encode())
                (dist / f"DevmemStudio-{version}-win64.zip.sha256").write_text("digest")
            plan = cleanup_workspace.build_plan(root, release_keep=2, days=3650, now=2_000_000_000)
            names = {item.path.name for item in plan}
            self.assertEqual(names, {"DevmemStudio-5.3.0-win64.zip", "DevmemStudio-5.3.0-win64.zip.sha256",
                                     "DevmemStudio-5.4.0-win64.zip", "DevmemStudio-5.4.0-win64.zip.sha256"})

    def test_artifact_plan_is_age_based_and_keeps_required_acceptance_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            old = artifacts / "old-run"
            old.mkdir()
            (old / "payload.bin").write_bytes(b"x" * 10)
            recent = artifacts / "recent-run"
            recent.mkdir()
            (recent / "report.json").write_text("{}")
            keep = artifacts / "build-smoke"
            keep.mkdir()
            old_time = 1_000_000_000
            recent_time = 2_000_000_000
            for path in (old, old / "payload.bin"):
                os.utime(path, (old_time, old_time))
            for path in (recent, recent / "report.json", keep):
                os.utime(path, (recent_time, recent_time))
            plan = cleanup_workspace.build_plan(root, days=10, now=recent_time)
            self.assertEqual([item.path.name for item in plan], ["old-run"])

    def test_apply_removes_only_planned_paths_under_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            item = root / "artifacts" / "old"
            item.mkdir(parents=True)
            payload = item / "data"
            payload.write_bytes(b"123")
            plan = [cleanup_workspace.Candidate(item, 3, "test")]
            self.assertEqual(cleanup_workspace.remove_candidates(root, plan), 3)
            self.assertFalse(item.exists())
            with self.assertRaises(ValueError):
                cleanup_workspace.remove_candidates(root, [cleanup_workspace.Candidate(root, 0, "bad")])


if __name__ == "__main__":
    unittest.main()
