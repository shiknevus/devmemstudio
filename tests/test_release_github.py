"""tools/release_github.py against an in-memory GitHub: draft first, publish after verification, rename on replace."""
import hashlib
import io
import json
from pathlib import Path
import re
import runpy
import shutil
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = "shiknevus/devmemstudio"
VERSION = "99.0.0"
NAME = f"DevmemStudio-{VERSION}-win64.zip"


class FakeGitHub:
    def __init__(self):
        self.releases, self.calls, self.exposed = {}, [], []
        self.ids = iter(range(100, 10_000))
        self.corrupt_size = False

    def release(self, draft, assets=(), tag=f"v{VERSION}"):
        number = next(self.ids)
        item = {"id": number, "tag_name": tag, "draft": draft, "assets": [], "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
                "target_commitish": "abc", "name": "x", "body": ""}
        self.releases[number] = item
        for name, data in assets:
            self.add_asset(item, name, data)
        return item

    def add_asset(self, release, name, data):
        number = next(self.ids)
        asset = {"id": number, "name": name, "size": len(data) + (1 if self.corrupt_size else 0), "state": "uploaded",
                 "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                 "url": f"https://api.github.com/repos/{REPO}/releases/assets/{number}"}
        release["assets"].append(asset)
        return asset

    def urlopen(self, request, timeout=None):
        url, method = request.full_url, request.get_method()
        body = json.loads(request.data) if request.data and "uploads." not in url else None
        status, result = self.handle(method, url, body, request.data)
        self.calls.append((method, url.split(REPO)[-1], status, body))
        for item in self.releases.values():   # what an update check sees after each call
            names = {asset["name"] for asset in item["assets"]}
            if not item["draft"] and not {NAME, NAME + ".sha256"} <= names:
                self.exposed.append((method, url.split(REPO)[-1]))
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps(result).encode() if result is not None else b"")

    def handle(self, method, url, body, raw):
        path = url.split(f"{REPO}/")[-1]
        if path.startswith("releases/tags/"):
            tag = path.rsplit("/", 1)[1]
            found = [item for item in self.releases.values() if item["tag_name"] == tag and not item["draft"]]
            return (200, found[0]) if found else (404, None)
        if path.startswith("releases?"):
            return 200, list(self.releases.values())
        if method == "POST" and path == "releases":
            item = self.release(body["draft"], tag=body["tag_name"])
            return 201, item
        if match := re.fullmatch(r"releases/(\d+)", path):
            item = self.releases[int(match.group(1))]
            if method == "PATCH":
                item.update(body)
            return 200, item
        if match := re.fullmatch(r"releases/(\d+)/assets\?name=(.+)", path):
            item, name = self.releases[int(match.group(1))], re.sub(r"%([0-9A-F]{2})", lambda m: chr(int(m.group(1), 16)), match.group(2))
            if any(asset["name"] == name for asset in item["assets"]):
                return 422, None
            return 201, self.add_asset(item, name, raw)
        if match := re.fullmatch(r"releases/assets/(\d+)", path):
            for item in self.releases.values():
                for asset in item["assets"]:
                    if asset["id"] == int(match.group(1)):
                        if method == "DELETE":
                            item["assets"].remove(asset)
                            return 204, None
                        if any(other["name"] == body["name"] for other in item["assets"]):
                            return 422, None
                        asset["name"] = body["name"]
                        return 200, asset
            return 404, None
        raise AssertionError(f"unexpected call {method} {url}")


class ReleaseScriptTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="devmem-release-gh-")
        base = Path(self.scratch.name)
        (base / "tools").mkdir()
        (base / "dist").mkdir()
        shutil.copyfile(ROOT / "tools" / "release_github.py", base / "tools" / "release_github.py")   # its ROOT is then this folder
        self.zip = b"PK-new-package"
        (base / "dist" / NAME).write_bytes(self.zip)
        self.sum = hashlib.sha256(self.zip).hexdigest().encode() + b"\n"
        (base / "dist" / (NAME + ".sha256")).write_bytes(self.sum)
        (base / "body.md").write_text("notes", encoding="utf-8")
        self.base, self.github = base, FakeGitHub()

    def tearDown(self):
        self.scratch.cleanup()

    def run_script(self, *extra):
        def run(command, **kwargs):
            if command[1:3] == ["credential", "fill"]:
                return types.SimpleNamespace(stdout=b"password=token\n")
            return types.SimpleNamespace(stdout="abc123\n")

        argv = ["release_github.py", VERSION, str(self.base / "body.md"), *extra]
        with patch.object(sys, "argv", argv), patch("subprocess.run", run), patch("time.sleep"), \
                patch("urllib.request.urlopen", self.github.urlopen):
            runpy.run_path(str(self.base / "tools" / "release_github.py"), run_name="__main__")

    def methods(self):
        return [(method, path.split("?")[0]) for method, path, *_ in self.github.calls]

    def test_new_release_stays_draft_until_every_asset_is_up(self):
        self.run_script()
        (release,) = self.github.releases.values()
        self.assertFalse(release["draft"])
        self.assertEqual(sorted(asset["name"] for asset in release["assets"]), [NAME, NAME + ".sha256"])
        created = next(body for method, path, _, body in self.github.calls if method == "POST" and path == "/releases")
        self.assertTrue(created["draft"])
        publish = [index for index, (_, _, _, body) in enumerate(self.github.calls) if body == {"draft": False, "prerelease": False}]
        uploads = [index for index, (method, path, *_) in enumerate(self.github.calls) if "/assets?name=" in path]
        self.assertEqual(len(publish), 1)
        self.assertEqual(len(uploads), 2)
        self.assertGreater(publish[0], max(uploads))
        self.assertEqual(self.github.exposed, [])   # never public without the package and its checksum

    def test_interrupted_draft_is_resumed_not_duplicated(self):
        draft = self.github.release(True, [(NAME, self.zip)])
        self.run_script()
        self.assertEqual(list(self.github.releases), [draft["id"]])
        self.assertFalse(draft["draft"])
        self.assertEqual(sorted(asset["name"] for asset in draft["assets"]), [NAME, NAME + ".sha256"])
        self.assertEqual([call for call in self.methods() if call == ("POST", "/releases")], [])
        self.assertEqual(self.github.exposed, [])

    def test_changed_asset_is_staged_then_old_deleted_then_renamed(self):
        release = self.github.release(False, [(NAME, b"old package"), (NAME + ".sha256", self.sum)])
        self.run_script()
        self.assertEqual(sorted(asset["name"] for asset in release["assets"]), [NAME, NAME + ".sha256"])
        package = next(asset for asset in release["assets"] if asset["name"] == NAME)
        self.assertEqual(package["digest"], "sha256:" + hashlib.sha256(self.zip).hexdigest())
        asset_calls = [method for method, path, *_ in self.github.calls if "/assets" in path]
        self.assertEqual(asset_calls, ["POST", "DELETE", "PATCH"])   # one upload, no second one
        self.assertEqual({method for method, _ in self.github.exposed}, {"DELETE"})   # only between the delete and the rename

    def test_mismatching_asset_leaves_the_release_a_draft(self):
        self.github.corrupt_size = True
        with self.assertRaises(SystemExit) as raised:
            self.run_script()
        self.assertIn("do not match", str(raised.exception))
        (release,) = self.github.releases.values()
        self.assertTrue(release["draft"])

    def test_status_mode_only_reads(self):
        self.github.release(False, [(NAME, self.zip)])
        with self.assertRaises(SystemExit):
            self.run_script("status")
        self.assertEqual({method for method, _ in self.methods()}, {"GET"})


if __name__ == "__main__":
    unittest.main()
