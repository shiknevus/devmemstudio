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


@unittest.skipUnless(sys.platform == "win32" and shutil.which("gcc"), "Windows and GCC required")
class WindowsReplacementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # MinGW itself cannot compile from a Unicode source path. The actual
        # installer below still runs in directories containing Chinese and spaces.
        cls.scratch = tempfile.TemporaryDirectory(prefix="devmem-update-compile-")
        cls.base = Path(cls.scratch.name)
        source = cls.base / "probe.c"
        source.write_text("#include <windows.h>\nint WINAPI WinMain(HINSTANCE a,HINSTANCE b,LPSTR c,int d){return EXIT_CODE;}\n")
        for name, code in (("ok", 0), ("fail", 7)):
            subprocess.run([shutil.which("gcc"), str(source), "-mwindows", f"-DEXIT_CODE={code}",
                            "-o", str(cls.base / f"{name}.exe")], check=True, capture_output=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def apply(self, directory, new="ok", digest=None, pids=None):
        base = Path(directory)
        target, staged, backup = (base / name for name in ("app.exe", "staged.exe", "app-1.0.0-backup.exe"))
        shutil.copyfile(self.base / "ok.exe", target)
        shutil.copyfile(self.base / f"{new}.exe", staged)
        (base / "registers.json").write_bytes(b"keep-settings")
        job = dict(target=str(target), staged=str(staged), backup=str(backup),
                   sha256=digest or updater.file_digest(staged), pids=pids or [], version="99.0.0")
        job_path = base / "job.json"
        job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        helper = ROOT / "assets/updater/apply_update.ps1"
        command = f". '{helper}'; $job = Get-Content -LiteralPath '{job_path}' -Raw -Encoding UTF8 | ConvertFrom-Json; Invoke-DevmemUpdate $job | ConvertTo-Json -Compress"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((base / "registers.json").read_bytes(), b"keep-settings")
        return json.loads(result.stdout), target, staged, backup

    def test_atomic_replacement_keeps_backup(self):
        with tempfile.TemporaryDirectory(prefix="安装 空格 ", dir=self.base) as directory:
            result, target, staged, backup = self.apply(directory)
            self.assertTrue(result["success"], result)
            self.assertEqual(backup.read_bytes(), (self.base / "ok.exe").read_bytes())
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
