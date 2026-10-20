"""Update regression tests, using in-memory HTTP and disposable Windows EXEs."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from devmem_studio import updater

ROOT = Path(__file__).resolve().parents[1]


def package_bytes(name="DevmemStudio/DevmemStudio.exe", content=b"MZtest-program"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr(name, content)
    return output.getvalue()


def release_payload(data=None):
    data = data or package_bytes()
    name = "DevmemStudio-99.0.0-win64.zip"
    return {"tag_name": "v99.0.0", "draft": False, "prerelease": False, "body": "Update notes",
            "assets": [{"name": name, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "browser_download_url": f"https://github.com/{updater.REPOSITORY}/releases/download/v99.0.0/{name}"}]}


class UpdateTransportTests(unittest.TestCase):
    def test_versions_are_numeric_and_stable(self):
        self.assertGreater(updater.version_tuple("v5.10.0"), updater.version_tuple("5.9.0"))
        for value in ("5.2.0-beta", "5.2", "latest", "../5.2.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                updater.version_tuple(value)

    def test_latest_or_older_returns_no_update(self):
        self.assertIsNone(updater.parse_release(release_payload(), current="99.0.0"))
        self.assertIsNone(updater.parse_release(release_payload(), current="100.0.0"))

    def test_missing_asset_or_checksum_blocks_update(self):
        for mutation in (lambda p: p.update(assets=[]), lambda p: p["assets"][0].update(digest=None),
                         lambda p: p.update(prerelease=True), lambda p: p["assets"][0].update(size=-1)):
            payload = release_payload()
            mutation(payload)
            with self.assertRaises(ValueError):
                updater.parse_release(payload)

    def test_foreign_repo_or_http_is_rejected(self):
        for url in ("http://github.com/a/b", "https://github.com/other/repo/releases/download/v99.0.0/app.zip",
                    "https://github.com.evil.test/app.zip", "https://user:password@github.com/app.zip"):
            payload = release_payload()
            payload["assets"][0]["browser_download_url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                updater.parse_release(payload)

    def test_sidecar_checksum_and_wrong_filename(self):
        payload = release_payload()
        asset = payload["assets"][0]
        digest = asset.pop("digest")[7:]
        checksum = copy.deepcopy(asset)
        checksum.update(name=asset["name"] + ".sha256", browser_download_url=asset["browser_download_url"] + ".sha256")
        payload["assets"].append(checksum)
        release = updater.parse_release(payload)
        self.assertTrue(release.checksum_url)
        self.assertEqual(updater.parse_checksum(f"{digest}  {release.name}\n".encode(), release.name), digest)
        with self.assertRaises(ValueError):
            updater.parse_checksum(f"{digest}  foreign.zip".encode(), release.name)

    def test_download_falls_back_to_sidecar_when_api_digest_is_missing(self):
        data = package_bytes()
        payload = release_payload(data)
        asset = payload["assets"][0]
        digest = asset.pop("digest")[7:]
        checksum = copy.deepcopy(asset)
        checksum.update(name=asset["name"] + ".sha256", browser_download_url=asset["browser_download_url"] + ".sha256")
        payload["assets"].append(checksum)
        responses = [io.BytesIO(f"{digest}  {asset['name']}\n".encode()), io.BytesIO(data)]
        with tempfile.TemporaryDirectory() as directory, patch.object(updater, "_open", side_effect=responses):
            result = updater.download_release(updater.parse_release(payload), directory=directory)
            self.assertEqual(result.executable.read_bytes(), b"MZtest-program")

    def test_download_verifies_and_reports_progress(self):
        data = package_bytes()
        release = updater.parse_release(release_payload(data))
        progress = []
        with tempfile.TemporaryDirectory() as directory, patch.object(updater, "_open", return_value=io.BytesIO(data)):
            result = updater.download_release(release, progress=progress.append, directory=directory)
            self.assertEqual(result.executable.read_bytes(), b"MZtest-program")
            self.assertEqual(result.digest, updater.file_digest(result.executable))
            self.assertEqual(progress[-1], (len(data), len(data)))
            self.assertFalse((result.executable.parent / "package.zip").exists())

    def test_mismatched_digest_or_size_cleans_partial_download(self):
        data = package_bytes()
        for received in (data[:-1], data + b"x", data[:-1] + bytes([data[-1] ^ 1])):
            with tempfile.TemporaryDirectory() as directory, patch.object(updater, "_open", return_value=io.BytesIO(received)):
                with self.assertRaises(ValueError):
                    updater.download_release(updater.parse_release(release_payload(data)), directory=directory)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancel_and_network_failure_clean_partial_download(self):
        cancel = threading.Event()
        cancel.set()
        release = updater.parse_release(release_payload())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(updater.UpdateCancelled):
                updater.download_release(release, cancel, directory=directory)
            with patch.object(updater, "_open", side_effect=TimeoutError("offline")), self.assertRaises(TimeoutError):
                updater.download_release(release, directory=directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancel_during_download_cleans_work_directory(self):
        cancel = threading.Event()
        data = package_bytes()
        with tempfile.TemporaryDirectory() as directory, patch.object(updater, "_open", return_value=io.BytesIO(data)):
            with self.assertRaises(updater.UpdateCancelled):
                updater.download_release(updater.parse_release(release_payload(data)), cancel,
                                         lambda value: cancel.set(), directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_check_shows_notes_of_every_version_since_current(self):
        def payload(tag, body, **extra):
            item = release_payload() if tag == "v99.0.0" else {"tag_name": tag, "assets": []}
            item.update({"tag_name": tag, "body": body, "draft": False, "prerelease": False, **extra})
            return item
        payloads = [payload("v98.0.0", "- middle"), payload("v99.0.0", "## 99.0.0 发布\n\n- newest"),
                    payload("v97.0.0", "- current"), payload("v96.0.0", "- older"),
                    payload("v98.5.0", "- draft", draft=True), payload("v98.6.0", "- beta", prerelease=True),
                    payload("v97.5.0", "")]
        data = json.dumps(payloads).encode()
        with patch.object(updater, "_open", return_value=io.BytesIO(data)):
            release = updater.check_release(current="97.0.0")
        self.assertEqual(release.version, "99.0.0")
        self.assertEqual(release.notes, "## 99.0.0 发布\n\n- newest\n\n## DevmemStudio 98.0.0\n\n- middle"
                                        "\n\n## DevmemStudio 97.5.0\n\n暂无更新说明。")
        with patch.object(updater, "_open", return_value=io.BytesIO(data)):
            self.assertIsNone(updater.check_release(current="99.0.0"))
        with patch.object(updater, "_open", return_value=io.BytesIO(b"[]")), self.assertRaises(updater.NotFound):
            updater.check_release()

    def test_notes_drop_release_page_sections(self):
        body = ("## DevmemStudio 99.0.0\n\n### 寄存器\n- fix `C:\\new`\n\n### 升级方式\n- click\n\n"
                "### 校验\n`x.zip` SHA-256：`ab`\n\n### 验证\n462 项")
        self.assertEqual(updater._dialog_markdown(body), "## DevmemStudio 99.0.0\n\n### 寄存器\n- fix `C:\\new`")
        self.assertEqual(updater._dialog_markdown("### 升级方式\n- click"), "")
        html = "<h3>寄存器</h3><ul><li>fix</li></ul><h3>升级方式</h3><ul><li>click</li></ul><h3>验证</h3><p>462</p>"
        self.assertEqual(updater._dialog_html(html), "<h3>寄存器</h3><ul><li>fix</li></ul>")

    def test_rate_limited_api_falls_back_to_latest_redirect(self):
        redirect = io.BytesIO()
        redirect.headers = {"Location": f"https://github.com/{updater.REPOSITORY}/releases/tag/v99.0.0"}
        with patch.object(updater, "_open", side_effect=[updater.RateLimited("quota"), redirect,
                                                         RuntimeError("feed offline")]):
            release = updater.check_release()
        self.assertEqual((release.version, release.size, release.digest), ("99.0.0", 0, ""))
        self.assertTrue(release.checksum_url.endswith("/releases/download/v99.0.0/DevmemStudio-99.0.0-win64.zip.sha256"))
        self.assertFalse(release.notes_html)
        self.assertIn("发布页", release.notes)
        redirect = io.BytesIO()
        redirect.headers = {"Location": f"https://github.com/{updater.REPOSITORY}/releases"}
        with patch.object(updater, "_open", side_effect=[updater.RateLimited("quota"), redirect]), \
                self.assertRaises(updater.NotFound):
            updater.check_release()

    @staticmethod
    def atom_feed(*entries):
        from xml.sax.saxutils import escape
        items = "".join(
            f'<entry><link rel="alternate" type="text/html" '
            f'href="https://github.com/{updater.REPOSITORY}/releases/tag/{tag}"/>'
            f'<title>DevmemStudio {tag}</title><content type="html">{escape(body)}</content></entry>'
            for tag, body in entries)
        return f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">{items}</feed>'.encode()

    def test_rate_limited_api_reads_notes_from_atom_feed(self):
        redirect = io.BytesIO()
        redirect.headers = {"Location": f"https://github.com/{updater.REPOSITORY}/releases/tag/v99.0.0"}
        feed = self.atom_feed(("v99.0.0", "<h2>DevmemStudio 99.0.0</h2><ul><li>newest</li></ul>"),
                              ("v98.0.0", "<p>middle</p>"), ("v97.5.0", ""), ("v97.0.0", "<p>current</p>"),
                              ("nightly", "<p>odd tag</p>"))
        with patch.object(updater, "_open", side_effect=[updater.RateLimited("quota"), redirect, io.BytesIO(feed)]):
            release = updater.check_release(current="97.0.0")
        self.assertTrue(release.notes_html)
        self.assertEqual(release.notes, "<h2>DevmemStudio 99.0.0</h2><ul><li>newest</li></ul>\n"
                                        "<h2>DevmemStudio 98.0.0</h2><p>middle</p>\n"
                                        "<h2>DevmemStudio 97.5.0</h2><p>暂无更新说明。</p>")

    def test_atom_feed_notes_flag_missing_versions(self):
        # Feed (newest 10 only) stops short of current, or lags behind the new release.
        feed = self.atom_feed(("v99.0.0", "<p>newest</p>"), ("v98.0.0", "<p>middle</p>"))
        self.assertIn("部分版本的说明未列出", updater.feed_notes(feed, "90.0.0", "99.0.0"))
        self.assertIn("部分版本的说明未列出", updater.feed_notes(feed, "97.0.0", "100.0.0"))
        feed = self.atom_feed(("v99.0.0", "<p>newest</p>"), ("v97.0.0", "<p>current</p>"))
        self.assertNotIn("部分版本的说明未列出", updater.feed_notes(feed, "97.0.0", "99.0.0"))
        self.assertEqual(updater.feed_notes(feed, "99.0.0", "99.0.0"), "")
        with self.assertRaises(SyntaxError):
            updater.feed_notes(b"<feed", "97.0.0", "99.0.0")

    def test_redirect_release_downloads_with_content_length_and_sidecar(self):
        data = package_bytes()
        release = updater.Release("99.0.0", "", "DevmemStudio-99.0.0-win64.zip",
                                  f"https://github.com/{updater.REPOSITORY}/releases/download/v99.0.0/x.zip", 0, "",
                                  f"https://github.com/{updater.REPOSITORY}/releases/download/v99.0.0/x.zip.sha256")
        for length, received in ((str(len(data)), data), ("", data), (str(len(data)), data[:-1])):
            package = io.BytesIO(received)
            package.headers = {"Content-Length": length}
            checksum = io.BytesIO(f"{hashlib.sha256(data).hexdigest()}  {release.name}".encode())
            progress = []
            with self.subTest(length=length, size=len(received)), tempfile.TemporaryDirectory() as directory, \
                    patch.object(updater, "_open", side_effect=[checksum, package]):
                if received != data:
                    with self.assertRaises(ValueError):
                        updater.download_release(release, directory=directory)
                    continue
                result = updater.download_release(release, progress=progress.append, directory=directory)
                self.assertEqual(result.executable.read_bytes(), b"MZtest-program")
                self.assertEqual(progress[-1], (len(data), int(length or 0)))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(updater, "_open", side_effect=updater.NotFound("missing")), self.assertRaises(ValueError):
            updater.download_release(release, directory=directory)

    def test_transient_connection_errors_are_retried(self):
        from urllib.error import URLError
        opener = unittest.mock.MagicMock()
        opener.open.side_effect = [URLError("EOF"), URLError("EOF"), io.BytesIO(b"{}")]
        with patch("urllib.request.build_opener", return_value=opener), patch.object(updater.time, "sleep"):
            self.assertEqual(updater._open(updater.API_URL).read(), b"{}")
        opener.open.side_effect = URLError("EOF")
        with patch("urllib.request.build_opener", return_value=opener), patch.object(updater.time, "sleep"), \
                self.assertRaises(RuntimeError):
            updater._open(updater.API_URL)

    def test_archive_traversal_and_non_exe_are_rejected(self):
        for name, content in (("../outside.exe", b"MZx"), ("DevmemStudio/DevmemStudio.exe", b"not-an-exe")):
            with tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "package.zip"
                archive.write_bytes(package_bytes(name, content))
                with self.assertRaises(ValueError):
                    updater.extract_executable(archive, Path(directory) / "new.exe")

    def test_source_mode_never_targets_python_executable(self):
        with patch.object(updater.sys, "frozen", False, create=True), self.assertRaises(ValueError):
            updater.launcher_path()

    def test_prepare_install_preserves_original_and_configuration(self):
        with tempfile.TemporaryDirectory(prefix="update 空格 ") as directory:
            base = Path(directory)
            target = base / "RenamedApp.exe"
            target.write_bytes(b"old")
            config = base / "registers.json"
            config.write_bytes(b"settings")
            work = base / "download"
            work.mkdir()
            new = work / "DevmemStudio.exe"
            new.write_bytes(b"MZnew")
            download = updater.Download("99.0.0", new, updater.file_digest(new))
            job_path = updater.prepare_install(download, target)
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(Path(job["staged"]).parent, target.parent)
            self.assertEqual(Path(job["staged"]).read_bytes(), new.read_bytes())
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(config.read_bytes(), b"settings")
            self.assertTrue((work / "apply_update.ps1").exists())
            self.assertEqual(Path(job["backup"]), base / f"RenamedApp-{updater.__version__}-backup.exe")
            updater.discard_install(job_path)
            self.assertFalse(Path(job["staged"]).exists())

    def test_backup_keeps_exe_extension_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "DevmemStudio.exe"
            first = updater.backup_path(target, "abcdef0123456789")
            self.assertEqual(first.name, f"DevmemStudio-{updater.__version__}-backup.exe")
            first.write_bytes(b"older backup")
            second = updater.backup_path(target, "abcdef0123456789")
            self.assertEqual(second.name, f"DevmemStudio-{updater.__version__}-backup-abcdef01.exe")

    def test_prepare_digest_failure_leaves_original_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "old.exe"
            target.write_bytes(b"old")
            new = base / "new.exe"
            new.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                updater.prepare_install(updater.Download("99.0.0", new, "0" * 64), target)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(list(base.glob(".DevmemStudio-update-*")), [])

    def test_latest_release_still_uploading_means_no_update_yet(self):
        uploading = release_payload()
        uploading["assets"] = []
        older = release_payload()
        older["tag_name"] = "v98.0.0"
        # A newer release whose package is missing/renamed must NOT read as
        # "up to date": the user is told the release is not ready to install.
        with patch.object(updater, "_open", return_value=io.BytesIO(json.dumps([uploading, older]).encode())):
            with self.assertRaises(updater.MissingAsset):
                updater.check_release(current="97.0.0")
        # When the newest formal release is not newer than the running version,
        # a missing asset is simply "no update" (nothing newer to announce).
        uploading["tag_name"] = "v97.0.0"
        with patch.object(updater, "_open", return_value=io.BytesIO(json.dumps([uploading]).encode())):
            self.assertIsNone(updater.check_release(current="97.0.0"))
        foreign = release_payload()
        foreign["assets"][0]["browser_download_url"] = "https://github.com/other/repo/releases/download/v99.0.0/x.zip"
        with patch.object(updater, "_open", return_value=io.BytesIO(json.dumps([foreign]).encode())), \
                self.assertRaises(ValueError):
            updater.check_release(current="97.0.0")

    def test_cleanup_failure_never_masks_download_error(self):
        data = package_bytes()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(updater, "_open", return_value=io.BytesIO(data[:-1])), \
                patch.object(updater.shutil, "rmtree") as rmtree, self.assertRaises(ValueError):
            updater.download_release(updater.parse_release(release_payload(data)), directory=directory)
        self.assertEqual(rmtree.call_args.kwargs, {"ignore_errors": True})

    def test_leftovers_are_cleaned_except_recent_kept_and_newest_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            updates, app = base / "updates", base / "app"
            app.mkdir()
            old = time.time() - 2 * updater.LEFTOVER_AGE
            for name in ("download-old", "download-fresh", "download-kept"):
                (updates / name).mkdir(parents=True)
                (updates / name / "DevmemStudio.exe").write_bytes(b"MZ")
                if name != "download-fresh":
                    os.utime(updates / name / "DevmemStudio.exe", (old, old))
                    os.utime(updates / name, (old, old))
            target = app / "Tool.exe"
            names = ["Tool.exe", f".DevmemStudio-update-{'a' * 32}.exe", f".DevmemStudio-update-{'b' * 32}.exe",
                     "Tool-5.9.0-backup.exe", "Tool-5.10.0-backup.exe", "Tool-5.10.0-backup-abcdef01.exe",
                     "Other-1.0.0-backup.exe", "notes.txt"]
            for index, name in enumerate(names):
                (app / name).write_bytes(b"MZ")
                if name != f".DevmemStudio-update-{'b' * 32}.exe":
                    os.utime(app / name, (old + index, old + index))
            removed = updater.cleanup_update_leftovers(target, keep=[updates / "download-kept"], directory=updates)
            self.assertEqual(sorted(item.name for item in updates.iterdir()), ["download-fresh", "download-kept"])
            self.assertEqual(sorted(item.name for item in app.iterdir()),
                             sorted(["Tool.exe", f".DevmemStudio-update-{'b' * 32}.exe",
                                     "Tool-5.10.0-backup-abcdef01.exe", "Other-1.0.0-backup.exe", "notes.txt"]))
            self.assertEqual(len(removed), 4)
            self.assertEqual(updater.cleanup_update_leftovers(target, directory=base / "missing"), [])

    def test_last_update_result_is_read_once_and_tolerates_unreadable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            updates = Path(directory)
            self.assertIsNone(updater.take_update_result(updates))
            for name, payload, stamp in (("download-a", "﻿" + json.dumps({"success": False, "error": "x"}), 1),
                                         ("download-b", "﻿" + json.dumps({"success": True, "version": "99.0.0"}), 2)):
                (updates / name).mkdir()
                (updates / name / "result.json").write_text(payload, encoding="utf-8")
                os.utime(updates / name / "result.json", (1_000_000 + stamp, 1_000_000 + stamp))
            result = updater.take_update_result(updates)
            self.assertEqual((result["success"], result["version"], result["restored"]), (True, "99.0.0", False))
            self.assertEqual(list(updates.glob("*/result.json")), [])
            self.assertIsNone(updater.take_update_result(updates))
            (updates / "download-a" / "result.json").write_bytes(b"\x93\x00encrypted\xff")
            self.assertIsNone(updater.take_update_result(updates))
            self.assertFalse((updates / "download-a" / "result.json").exists())

    def start_with(self, script, timeout=5.0, refuse_breakaway=False):
        directory = Path(tempfile.mkdtemp(prefix="devmem-start-"))
        self.addCleanup(shutil.rmtree, directory, True)
        marker = directory / "started.flag"
        job_path = directory / "install.json"
        job_path.write_text(json.dumps({"started": str(marker)}), encoding="utf-8")
        real, calls = subprocess.Popen, []

        def popen(command, **options):
            calls.append(options["creationflags"])
            if refuse_breakaway and options["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError(5, "Access is denied")
            return real([sys.executable, "-c", script.replace("MARKER", repr(str(marker)))], **options)

        with patch.object(updater.subprocess, "Popen", side_effect=popen):
            started = time.monotonic()
            try:
                return updater.start_install(job_path, timeout=timeout), time.monotonic() - started, calls
            except OSError as exc:
                return exc, time.monotonic() - started, calls

    def test_start_install_returns_once_helper_marks_started(self):
        process, seconds, calls = self.start_with("import time; open(MARKER, 'w').close(); time.sleep(0.5)")
        self.assertNotIsInstance(process, OSError)
        process.wait(10)
        self.assertTrue(calls[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        process, seconds, calls = self.start_with("open(MARKER, 'w').close()", refuse_breakaway=True)
        self.assertNotIsInstance(process, OSError)
        process.wait(10)
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB)

    def test_blocked_helper_raises_so_the_app_stays_open(self):
        error, seconds, _ = self.start_with("raise SystemExit(1)")   # AllSigned: script refused at once
        self.assertIsInstance(error, OSError)
        self.assertLess(seconds, 4)
        error, seconds, _ = self.start_with("import time; time.sleep(30)", timeout=1)   # hung helper is killed
        self.assertIsInstance(error, OSError)
        self.assertLess(seconds, 6)

    def test_pump_callback_keeps_the_poll_throttled_not_a_busy_wait(self):
        # The pump must only keep the caller's UI repainting; it must NOT replace
        # the 50 ms sleep and turn the marker-stat loop into a CPU-burning spin
        # while PowerShell takes a couple of seconds to start.
        directory = Path(tempfile.mkdtemp(prefix="devmem-pump-"))
        self.addCleanup(shutil.rmtree, directory, True)
        marker = directory / "started.flag"
        job_path = directory / "install.json"
        job_path.write_text(json.dumps({"started": str(marker)}), encoding="utf-8")
        script = "import time; time.sleep(0.3); open(%r, 'w').close()" % str(marker)
        real = subprocess.Popen

        def popen(command, **options):
            return real([sys.executable, "-c", script], **options)

        pump_count = [0]

        def pump():
            pump_count[0] += 1

        with patch.object(updater.subprocess, "Popen", side_effect=popen):
            updater.start_install(job_path, timeout=5.0, pump=pump)
        # 0.3 s at a 50 ms cadence is ~6-8 polls; an unthrottled busy wait would
        # run thousands of times.  Keep a generous bound to stay robust on slow CI.
        self.assertLessEqual(pump_count[0], 40)


@unittest.skipUnless(sys.platform == "win32" and shutil.which("gcc") and shutil.which("windres"), "MinGW required")
class ReleaseVersionTests(unittest.TestCase):
    def test_release_refuses_executable_of_another_version(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import make_release
        finally:
            sys.path.remove(str(ROOT / "tools"))
        with tempfile.TemporaryDirectory(prefix="devmem-release-") as directory:
            base = Path(directory)
            (base / "main.c").write_text("int main(void){return 0;}\n")
            (base / "version.rc").write_text('1 VERSIONINFO\nFILEVERSION 5,2,7,0\nPRODUCTVERSION 5,2,7,0\nBEGIN\nEND\n')
            subprocess.run([shutil.which("windres"), "version.rc", "-O", "coff", "-o", "version.res"],
                           cwd=base, check=True, capture_output=True, timeout=30)
            exe = base / "dist" / "DevmemStudio.exe"
            exe.parent.mkdir()
            subprocess.run([shutil.which("gcc"), "main.c", "version.res", "-o", str(exe)],
                           cwd=base, check=True, capture_output=True, timeout=30)
            self.assertEqual(make_release.exe_version(exe), "5.2.7")
            with self.assertRaises(SystemExit):
                make_release.build_release(exe, "5.2.8")
            self.assertEqual(sorted(item.name for item in exe.parent.iterdir()), ["DevmemStudio.exe"])
            report = make_release.build_release(exe, "5.2.7")
            with zipfile.ZipFile(report["archive"]) as package:
                self.assertEqual(package.namelist(), ["DevmemStudio/DevmemStudio.exe"])
            (base / "plain.c").write_text("int main(void){return 0;}\n")
            subprocess.run([shutil.which("gcc"), "plain.c", "-o", "plain.exe"], cwd=base, check=True,
                           capture_output=True, timeout=30)
            with self.assertRaises(SystemExit):
                make_release.build_release(base / "plain.exe", "5.2.7")


# ok/fail exit at once; window = launcher error box; stuck = no app ever; parent = launcher running its app.
PROBE_SOURCE = r"""#include <windows.h>
#include <string.h>
#ifndef EXIT_CODE
#define EXIT_CODE 0
#endif
int WINAPI WinMain(HINSTANCE a, HINSTANCE b, LPSTR c, int d)
{
#if defined(WINDOW)
    CreateWindowExW(WS_EX_TOOLWINDOW, L"STATIC", L"probe error", WS_POPUP | WS_VISIBLE, -32000, -32000, 1, 1, 0, 0, a, 0);
    Sleep(30000);
#elif defined(STUCK)
    Sleep(30000);
#elif defined(PARENT)
    if (strstr(c, "child")) { Sleep(30000); return 0; }
    wchar_t self[MAX_PATH], line[MAX_PATH + 16];
    GetModuleFileNameW(0, self, MAX_PATH);
    wsprintfW(line, L"\"%s\" child", self);
    STARTUPINFOW si = {sizeof si};
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(self, line, 0, 0, 0, 0, 0, 0, &si, &pi)) return 9;
    Sleep(30000);
#endif
    return EXIT_CODE;
}
"""


def kill_tree(pid):
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


@unittest.skipUnless(sys.platform == "win32" and shutil.which("gcc"), "Windows and GCC required")
class WindowsReplacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # MinGW itself cannot compile from a Unicode source path. The actual
        # installer below still runs in directories containing Chinese and spaces.
        cls.scratch = tempfile.TemporaryDirectory(prefix="devmem-update-compile-")
        cls.base = Path(cls.scratch.name)
        source = cls.base / "probe.c"
        source.write_text(PROBE_SOURCE)
        for name, flags in (("ok", ["-DEXIT_CODE=0"]), ("fail", ["-DEXIT_CODE=7"]), ("window", ["-DWINDOW"]),
                            ("stuck", ["-DSTUCK"]), ("parent", ["-DPARENT"])):
            subprocess.run([shutil.which("gcc"), str(source), "-mwindows", *flags,
                            "-o", str(cls.base / f"{name}.exe")], check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def apply(self, directory, new="ok", digest=None, pids=None, **extra):
        base = Path(directory)
        target, staged, backup = (base / name for name in ("app.exe", "staged.exe", "app-1.0.0-backup.exe"))
        if not target.exists():
            shutil.copyfile(self.base / "ok.exe", target)
        shutil.copyfile(self.base / f"{new}.exe", staged)
        (base / "registers.json").write_bytes(b"keep-settings")
        job = dict(target=str(target), staged=str(staged), backup=str(backup),
                   sha256=digest or updater.file_digest(staged), pids=pids or [], version="99.0.0")
        job.update(extra)
        job_path = base / "job.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        helper = ROOT / "assets/updater/apply_update.ps1"
        command = f". '{helper}'; $job = Get-Content -LiteralPath '{job_path}' -Raw -Encoding UTF8 | ConvertFrom-Json; Invoke-DevmemUpdate $job | ConvertTo-Json -Compress"
        # Files, not pipes: a still-running new program inherits them and would hold a pipe open.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            code = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                  stdout=stdout, stderr=stderr, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW).returncode
            stdout.seek(0)
            stderr.seek(0)
            output, errors = (item.read().decode("utf-8", "replace") for item in (stdout, stderr))
        self.assertEqual(code, 0, errors)
        self.assertEqual((base / "registers.json").read_bytes(), b"keep-settings")
        return json.loads(output), target, staged, backup

    def test_atomic_replacement_keeps_backup(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            result, target, staged, backup = self.apply(directory)
            self.assertTrue(result["success"], result)
            self.assertEqual(backup.read_bytes(), (self.base / "ok.exe").read_bytes())
            self.assertFalse(staged.exists())

    def test_bracketed_install_path_starts_new_program(self):
        with tempfile.TemporaryDirectory(prefix="工具[新] ", dir=self.base) as directory:
            result, target, staged, backup = self.apply(directory)
            self.assertTrue(result["success"], result)
            self.assertFalse(staged.exists())

    def test_bad_checksum_preserves_old_program(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            result, target, staged, backup = self.apply(directory, digest="0" * 64)
            self.assertFalse(result["success"])
            self.assertEqual(target.read_bytes(), (self.base / "ok.exe").read_bytes())
            self.assertFalse(backup.exists())
            self.assertFalse(staged.exists())

    def test_immediate_startup_failure_restores_old_program(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            result, target, staged, backup = self.apply(directory, new="fail")
            self.assertFalse(result["success"], result)
            self.assertTrue(result["restored"], result)
            self.assertEqual(target.read_bytes(), (self.base / "ok.exe").read_bytes())
            self.assertEqual(staged.read_bytes(), (self.base / "fail.exe").read_bytes())

    def test_new_launcher_error_box_rolls_back(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            result, target, staged, backup = self.apply(directory, new="window")
            self.assertFalse(result["success"], result)
            self.assertIn("showed an error", result["error"])
            self.assertTrue(result["restored"], result)
            self.assertEqual(target.read_bytes(), (self.base / "ok.exe").read_bytes())
            self.assertEqual(staged.read_bytes(), (self.base / "window.exe").read_bytes())

    def test_new_launcher_that_never_starts_app_rolls_back(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            result, target, staged, backup = self.apply(directory, new="stuck", start_timeout=4)
            self.assertFalse(result["success"], result)
            self.assertIn("did not start", result["error"])
            self.assertTrue(result["restored"], result)
            self.assertEqual(target.read_bytes(), (self.base / "ok.exe").read_bytes())

    def test_running_app_child_counts_as_started(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            result, target, staged, backup = self.apply(directory, new="parent", start_timeout=20)
            try:
                self.assertTrue(result["success"], result)
                self.assertEqual(target.read_bytes(), (self.base / "parent.exe").read_bytes())
            finally:
                kill_tree(result.get("pid", 0))
                time.sleep(0.5)   # image lock released before the directory is removed

    def test_access_denied_replace_is_retried(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            target = Path(directory) / "app.exe"
            shutil.copyfile(self.base / "ok.exe", target)
            os.chmod(target, 0o444)   # ReplaceFile: ERROR_ACCESS_DENIED until writable again
            timer = threading.Timer(1.5, os.chmod, (target, 0o666))
            timer.start()
            try:
                started = time.monotonic()
                result, target, staged, backup = self.apply(directory)
            finally:
                timer.join()
            self.assertTrue(result["success"], result)
            self.assertGreaterEqual(time.monotonic() - started, 1.4)

    def test_pinned_signer_rejects_unsigned_update_and_preserves_old(self):
        """When a signer thumbprint is pinned, an unsigned staged EXE must be
        refused before the swap: the old program stays, no backup is created,
        and the failure carries the signer message."""
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            base = Path(directory)
            old = (self.base / "ok.exe").read_bytes()
            result, target, staged, backup = self.apply(
                directory, expected_signer="A" * 40, digest=updater.file_digest(self.base / "ok.exe"))
            self.assertFalse(result["success"], result)
            self.assertIn("Authenticode", result["error"])
            self.assertEqual(target.read_bytes(), old)
            self.assertFalse(backup.exists())
            self.assertFalse(staged.exists())   # owned stage cleaned up

    def test_unsigned_update_allowed_when_signer_not_pinned(self):
        """Legacy behavior: without an expected_signer the unsigned staged EXE
        passes the SHA-256 check and installs normally (pinning is opt-in)."""
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            result, target, staged, backup = self.apply(directory)
            self.assertTrue(result["success"], result)
            self.assertEqual(target.read_bytes(), (self.base / "ok.exe").read_bytes())

    def test_malformed_job_reports_failure_and_removes_owned_stage(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            base = Path(directory)
            staged = base / f".DevmemStudio-update-{'a' * 32}.exe"
            shutil.copyfile(self.base / "ok.exe", staged)
            job_path = base / "job.json"
            job_path.write_text(json.dumps({"staged": str(staged), "pids": []}), encoding="utf-8")
            helper = ROOT / "assets/updater/apply_update.ps1"
            command = (f". '{helper}'; $job = Get-Content -LiteralPath '{job_path}' -Raw -Encoding UTF8 | ConvertFrom-Json; "
                       "Invoke-DevmemUpdate $job | ConvertTo-Json -Compress")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["success"])
            self.assertFalse(staged.exists())

    def test_start_install_waits_for_helper_and_reports_result(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            base = Path(directory)
            target = base / "DevmemStudio.exe"
            shutil.copyfile(self.base / "ok.exe", target)
            updates = base / "updates"
            work = updates / "download-test"
            work.mkdir(parents=True)
            new = work / "DevmemStudio.exe"
            shutil.copyfile(self.base / "ok.exe", new)
            job_path = updater.prepare_install(updater.Download("99.0.0", new, updater.file_digest(new)), target)
            job = json.loads(job_path.read_text(encoding="utf-8"))
            job["pids"] = []   # this test process stays alive
            job_path.write_text(json.dumps(job), encoding="utf-8")
            process = updater.start_install(job_path, timeout=30)
            self.assertTrue(Path(job["started"]).exists())
            self.assertEqual(process.wait(60), 0)
            outcome = updater.take_update_result(updates)
            if outcome is not None:   # office DLP may encrypt PowerShell-written JSON
                self.assertTrue(outcome["success"], outcome)
                self.assertEqual(outcome["version"], "99.0.0")
            self.assertEqual(list(updates.glob("*/result.json")), [])
            self.assertTrue(Path(job["backup"]).exists())
            self.assertFalse(Path(job["staged"]).exists())

    def test_waits_for_existing_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"], creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
                started = time.monotonic()
                result, *_ = self.apply(directory, pids=[process.pid])
                self.assertTrue(result["success"], result)
                self.assertGreaterEqual(time.monotonic() - started, 2)
                self.assertIsNotNone(process.poll())
        finally:
            process.wait(timeout=10)


class UpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def settle(self, dialog):
        deadline = time.monotonic() + 5
        while dialog.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertFalse(dialog.busy)

    def test_check_shows_notes_and_download_action(self):
        from devmem_studio.update_dialog import UpdateDialog
        dialog = UpdateDialog()
        release = updater.parse_release(release_payload())
        self.assertEqual(dialog.action_button.text(), "检查更新")
        with patch.object(updater, "check_release", return_value=release):
            dialog.check()
            self.settle(dialog)
        self.assertEqual(dialog.notes.toPlainText(), release.notes)
        self.assertEqual(dialog.latest_value.text(), "99.0.0")
        self.assertEqual(dialog.action_button.text(), "下载更新")
        self.assertTrue(dialog.action_button.isEnabled())
        self.assertTrue(dialog.cancel_button.isHidden())
        dialog.download = updater.Download("99.0.0", Path("DevmemStudio.exe"), "0" * 64)
        dialog._refresh()
        self.assertEqual(dialog.action_button.text(), "重启并升级")
        with patch.object(updater, "check_release", return_value=None):
            dialog.release = dialog.download = None
            dialog.check()
            self.settle(dialog)
        self.assertEqual(dialog.action_button.text(), "重新检查")
        self.assertEqual(dialog.status.property("state"), "ok")
        dialog.close()

    def test_feed_html_notes_render_as_rich_text(self):
        from PySide6.QtCore import Qt
        from devmem_studio.update_dialog import UpdateDialog
        dialog = UpdateDialog()
        release = updater.Release("99.0.0", "<h2>DevmemStudio 99.0.0</h2><ul><li>newest</li></ul>",
                                  "DevmemStudio-99.0.0-win64.zip", "", 0, notes_html=True)
        with patch.object(updater, "check_release", return_value=release):
            dialog.check()
            self.settle(dialog)
        self.assertEqual(dialog.notes.toPlainText().split("\n"), ["DevmemStudio 99.0.0", "newest"])
        self.assertEqual(dialog.notes.document().lastBlock().blockFormat().alignment(), Qt.AlignLeft)
        dialog.close()

    def test_notes_render_left_with_heading_hierarchy(self):
        """Notes are left-aligned; version headings outrank section headings; inline code is styled."""
        from PySide6.QtCore import Qt
        from devmem_studio.update_dialog import UpdateDialog
        dialog = UpdateDialog()
        release = updater.Release("99.0.0", "## DevmemStudio 99.0.0\n\n### 寄存器\n\n- item `CODE`\n\n"
                                  "## DevmemStudio 98.0.0\n\n- older", "DevmemStudio-99.0.0-win64.zip", "", 0)
        with patch.object(updater, "check_release", return_value=release):
            dialog.check()
            self.settle(dialog)
        self.assertEqual(dialog.notes_title.alignment() & Qt.AlignHorizontal_Mask, Qt.AlignLeft)
        blocks, block = {}, dialog.notes.document().begin()
        while block.isValid():
            self.assertEqual(block.blockFormat().alignment(), Qt.AlignLeft, block.text())
            blocks[block.text()] = block
            block = block.next()
        size = {text: item.begin().fragment().charFormat().font().pointSizeF() for text, item in blocks.items()}
        self.assertGreater(size["DevmemStudio 99.0.0"], size["寄存器"])
        self.assertEqual(blocks["DevmemStudio 99.0.0"].blockFormat().topMargin(), 0)
        self.assertGreater(blocks["DevmemStudio 98.0.0"].blockFormat().topMargin(), 0)
        code = [item.fragment() for item in blocks["item CODE"] if item.fragment().text() == "CODE"][0]
        self.assertIn("Consolas", code.charFormat().fontFamilies())
        dialog.close()

    def test_automatic_network_failure_stays_hidden(self):
        from devmem_studio.update_dialog import UpdateDialog
        dialog = UpdateDialog()
        with patch.object(updater, "check_release", side_effect=RuntimeError("offline")):
            dialog.check(automatic=True)
            self.settle(dialog)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.status.text(), "offline")
        self.assertEqual(dialog.status.property("state"), "error")
        self.assertTrue(dialog.action_button.isEnabled())
        dialog.close()

    def test_window_can_minimize_and_reopens_restored(self):
        from PySide6.QtCore import Qt
        from devmem_studio.update_dialog import UpdateDialog
        dialog = UpdateDialog()
        self.assertTrue(dialog.windowFlags() & Qt.WindowMinimizeButtonHint)
        dialog.show()
        dialog.setWindowState(Qt.WindowMinimized)
        dialog.present()
        self.assertFalse(dialog.windowState() & Qt.WindowMinimized)
        self.assertTrue(dialog.isVisible())
        dialog.close()

    def drain(self, seconds=0.3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)

    def test_escape_closes_at_once_while_request_is_blocked(self):
        from devmem_studio.update_dialog import UpdateDialog
        dialog = UpdateDialog()
        dialog.show()
        entered, release = threading.Event(), threading.Event()

        def check(cancel):
            entered.set()
            release.wait(5)   # blocked socket: does not look at the cancel flag
            raise RuntimeError("late failure")

        with patch.object(updater, "check_release", side_effect=check):
            dialog.check()
            self.assertTrue(entered.wait(2))
            dialog.reject()
            self.assertTrue(dialog._cancel.is_set())
            self.assertFalse(dialog.busy)
            self.assertFalse(dialog.isVisible())
            release.set()
            self.drain()
        self.assertEqual(dialog.status.text(), "已取消检查。")   # the late failure is ignored

    def test_cancelled_download_is_discarded_and_new_check_starts_immediately(self):
        from devmem_studio.update_dialog import UpdateDialog
        dialog = UpdateDialog()
        dialog.release = updater.parse_release(release_payload())
        entered, release = threading.Event(), threading.Event()
        work = Path(tempfile.mkdtemp(prefix="devmem-update-late-"))
        (work / "DevmemStudio.exe").write_bytes(b"MZ")

        def download(item, cancel, progress):
            entered.set()
            release.wait(5)
            return updater.Download("99.0.0", work / "DevmemStudio.exe", "0" * 64)

        with patch.object(updater, "download_release", side_effect=download):
            dialog.take_action()
            self.assertTrue(entered.wait(2))
            self.assertEqual(dialog.action_button.text(), "正在下载…")
            dialog.cancel()
            self.assertFalse(dialog.busy)
            self.assertTrue(dialog.progress_panel.isHidden())
            self.assertEqual(dialog.action_button.text(), "下载更新")
        with patch.object(updater, "check_release", return_value=None):
            dialog.release = None
            dialog.check()   # not queued behind the abandoned download
            self.settle(dialog)
        self.assertEqual(dialog.action_button.text(), "重新检查")
        release.set()
        self.drain()
        self.assertIsNone(dialog.download)
        self.assertFalse(work.exists())
        dialog.close()


class UpdateWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from devmem_studio.core import ConfigStore
        from devmem_studio.window import MainWindow
        self.scratch = tempfile.TemporaryDirectory(prefix="devmem-update-window-")
        self.window = MainWindow(ConfigStore(Path(self.scratch.name) / "registers.json"), persist=False)

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.scratch.cleanup()

    def test_auto_check_can_be_disabled_and_never_runs_in_offline_acceptance(self):
        self.window.check_updates_automatically()
        self.assertIsNone(self.window.update_dialog)
        self.window.persist = True
        self.window.cfg["check_updates_on_start"] = False
        self.window.check_updates_automatically()
        self.assertIsNone(self.window.update_dialog)

    def test_settings_failure_prevents_install_preparation(self):
        from PySide6.QtWidgets import QMessageBox
        with patch.object(self.window, "save_settings", return_value=False), \
                patch.object(updater, "prepare_install") as prepare, patch.object(QMessageBox, "warning"):
            self.window._request_update_install(object())
        prepare.assert_not_called()
        self.assertIsNone(self.window._pending_update)
        self.assertFalse(self.window._closing)

    def test_helper_spawn_failure_cancels_install_and_keeps_window_open(self):
        from PySide6.QtWidgets import QMessageBox
        self.window.show()
        with patch.object(updater, "prepare_install", return_value=Path(self.scratch.name) / "job.json"), \
                patch.object(updater, "start_install", side_effect=OSError("PowerShell unavailable")), \
                patch.object(updater, "discard_install") as discard, patch.object(QMessageBox, "warning"):
            self.window._request_update_install(object())
        discard.assert_called_once()
        self.assertIsNone(self.window._pending_update)
        self.assertFalse(self.window._closing)
        self.assertTrue(self.window.isVisible())

    def test_main_window_closes_without_waiting_for_blocked_update_request(self):
        dialog = self.window._update_dialog()
        self.window.show()
        entered, release = threading.Event(), threading.Event()

        def check(cancel):
            entered.set()
            release.wait(5)   # a socket stuck until its timeout ignores the cancel flag
            raise RuntimeError("timed out")

        try:
            with patch.object(updater, "check_release", side_effect=check):
                dialog.check()
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                self.assertTrue(self.window.close())
                self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(dialog.busy)
            self.assertTrue(dialog._cancel.is_set())
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
