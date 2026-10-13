"""Selected bit/bin bundles, cancellable scans and indefinite SSH retry."""
from pathlib import Path
import os
import errno
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from devmem_studio.bitpack import firmware_members, run_firmware_pack, scan_firmware_files
from devmem_studio.core import CommandError, SshSession, connect_ssh_until_cancelled
import paramiko


class FirmwarePackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bit = self.root / "original.bit"
        self.bit.write_bytes(b"fpga source")
        self.bins = []
        for version in ("V1.0", "V2.0"):
            folder = self.root / version
            folder.mkdir()
            binary = folder / "从板固件.bin"
            binary.write_bytes(version.encode() * 1024)
            self.bins.append((binary, version))
        self.cancel = threading.Event()

    def pack(self, source=None, bins=None, progress=lambda _: None, timeout=30):
        return run_firmware_pack(source, self.bins if bins is None else bins, self.root,
                                 "A100", timeout, self.cancel, progress)

    def test_mixed_zip_retains_bin_names_in_version_folders_and_source_bytes(self):
        before = {path: path.read_bytes() for path in (self.bit, *(p for p, _ in self.bins))}
        result = self.pack(self.bit)
        self.assertEqual(result["code"], 0)
        with ZipFile(result["path"]) as archive:
            self.assertEqual(archive.namelist(), ["sunny_fpga.bit", "V1.0/从板固件.bin", "V2.0/从板固件.bin"])
            self.assertEqual(archive.read("sunny_fpga.bit"), before[self.bit])
            for path, version in self.bins:
                self.assertEqual(archive.read(f"{version}/{path.name}"), before[path])
            self.assertIsNone(archive.testzip())
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_only_one_selected_bin_omits_bit_and_unselected_binary(self):
        binary = self.bins[0][0]
        result = self.pack(bins=[(binary, "")])
        with ZipFile(result["path"]) as archive:
            self.assertEqual(archive.namelist(), [binary.name])
            self.assertEqual(archive.read(binary.name), binary.read_bytes())

    def test_multiple_bins_require_unique_safe_versions(self):
        first, second = (entry[0] for entry in self.bins)
        for versions in (("", ""), ("v1", "V1"), ("../escape", "v2"), ("CON", "v2"), ("v1.", "v2")):
            with self.subTest(versions=versions), self.assertRaises(ValueError):
                firmware_members(self.bit, [(first, versions[0]), (second, versions[1])])
        with self.assertRaises(ValueError):
            firmware_members(None, [])
        self.assertEqual(list(self.root.glob("*.zip")), [])

    def test_cancel_during_write_or_verification_leaves_no_output_or_temporary(self):
        for phase in ("正在打包", "正在校验"):
            with self.subTest(phase=phase):
                self.cancel.clear()
                result = self.pack(self.bit, progress=lambda line: self.cancel.set() if line.startswith(phase) else None)
                self.assertEqual(result["code"], 3)
                self.assertEqual(list(self.root.glob("*.zip")), [])
                self.assertEqual(list(self.root.glob(".bitpack-*.tmp")), [])
                self.assertEqual(self.bit.read_bytes(), b"fpga source")

    def test_timeout_does_not_publish(self):
        self.assertEqual(self.pack(self.bit, timeout=0)["code"], 4)
        self.assertEqual(list(self.root.glob("*.zip")), [])

    def test_same_second_outputs_are_unique_and_existing_package_survives_failure(self):
        with patch("devmem_studio.bitpack.time.strftime", return_value="20261013_123456"):
            first = Path(self.pack(self.bit)["path"])
            original = first.read_bytes()
            second = Path(self.pack(self.bit)["path"])
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), original)
            self.assertNotIn("-", first.name)
            publish = "os.rename" if os.name == "nt" else "os.link"
            with patch("devmem_studio.bitpack." + publish, side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    self.pack(self.bit)
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(list(self.root.glob(".bitpack-*.tmp")), [])

    def test_scan_finds_both_types_recursively_and_can_be_cancelled(self):
        upper = self.root / "firmware.BIN"
        upper.write_bytes(b"bin")
        (self.root / "skip.txt").write_text("ignore")
        result = scan_firmware_files(self.root, self.cancel, lambda _: None)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["files"], [str(self.bit)])
        self.assertEqual(set(result["bins"]), {str(upper), *(str(path) for path, _ in self.bins)})
        self.cancel.set()
        self.assertEqual(scan_firmware_files(self.root, self.cancel, lambda _: None)["code"], 3)


class SshRetryTests(unittest.TestCase):
    def test_default_retry_delay_is_fast(self):
        from devmem_studio.core import SSH_HANDSHAKE_TIMEOUT_S, SSH_RETRY_DELAY_S, SSH_RETRY_BANNER_TIMEOUT_S
        self.assertLessEqual(SSH_RETRY_DELAY_S, .25)
        self.assertLessEqual(SSH_RETRY_BANNER_TIMEOUT_S, 1.5)
        self.assertLessEqual(SSH_HANDSHAKE_TIMEOUT_S, 6.0)

    def test_network_failures_keep_retrying_then_connect(self):
        session = Mock()
        session.connect.side_effect = [TimeoutError("timeout"), ConnectionRefusedError("not ready"),
                                       CommandError("SSH 连接已断开，设备可能正在重启。"), None]
        progress = Mock()
        self.assertTrue(connect_ssh_until_cancelled(session, ("host", 22, "root", "pw", 8),
                                                  threading.Event(), progress, retry_delay=0))
        self.assertEqual(session.connect.call_count, 4)
        self.assertEqual(progress.call_count, 3)

    def test_cancel_during_retry_wait_ends_promptly(self):
        session = Mock()
        session.connect.side_effect = TimeoutError("timeout")
        cancel = threading.Event()
        with self.assertRaisesRegex(CommandError, "已取消"):
            connect_ssh_until_cancelled(session, (), cancel, lambda _: cancel.set(), retry_delay=30)
        self.assertEqual(session.connect.call_count, 1)

    def test_cancel_racing_success_closes_the_new_connection(self):
        cancel = threading.Event()
        session = Mock()
        session.connect.side_effect = lambda: cancel.set()
        with self.assertRaisesRegex(CommandError, "已取消"):
            connect_ssh_until_cancelled(session, (), cancel, Mock(), retry_delay=0)
        session.close.assert_called_once()

    def test_cancel_before_start_never_connects(self):
        cancel = threading.Event()
        cancel.set()
        session = Mock()
        with self.assertRaisesRegex(CommandError, "已取消"):
            connect_ssh_until_cancelled(session, (), cancel, Mock())
        session.connect.assert_not_called()

    def test_authentication_failure_raises_immediately_without_retrying(self):
        # Wrong credentials never fix themselves: retrying would hammer the
        # board's login handler, so the loop must stop at the first attempt.
        session = Mock()
        session.connect.side_effect = paramiko.AuthenticationException("Authentication failed.")
        progress = Mock()
        with self.assertRaisesRegex(CommandError, "认证失败"):
            connect_ssh_until_cancelled(session, ("host", 22, "root", "wrong", 8),
                                        threading.Event(), progress, retry_delay=0)
        session.connect.assert_called_once()
        progress.assert_not_called()

    def test_retry_backoff_grows_but_stays_capped(self):
        from devmem_studio.core import SSH_RETRY_MAX_DELAY_S
        self.assertLessEqual(SSH_RETRY_MAX_DELAY_S, 2.0)
        session = Mock()
        session.connect.side_effect = [ConnectionRefusedError("not yet")] * 10 + [None]
        cancel = Mock()
        cancel.is_set.return_value = False
        waits = []

        def record_wait(delay):
            waits.append(delay)
            return False

        cancel.wait.side_effect = record_wait
        self.assertTrue(connect_ssh_until_cancelled(session, (), cancel, Mock(), retry_delay=0.1))
        expected = [min(0.1 * 2 ** index, SSH_RETRY_MAX_DELAY_S) for index in range(len(waits))]
        self.assertEqual(waits, expected)
        self.assertGreaterEqual(waits[-1], SSH_RETRY_MAX_DELAY_S)


class SshSocketRecoveryTests(unittest.TestCase):
    def test_failed_banner_attempt_shuts_down_socket_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            session = SshSession(data_dir=Path(directory))
            client, sock = Mock(), Mock()
            client.connect.side_effect = TimeoutError("Error reading SSH protocol banner")
            with patch("devmem_studio.core.paramiko.SSHClient", return_value=client), \
                    patch.object(session, "_connect_socket_abortably", return_value=sock):
                with self.assertRaises(TimeoutError):
                    session.connect("127.0.0.1", 22, "root", "pw", timeout=2, handshake_timeout=30)
            sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
            sock.close.assert_called_once()
            client.close.assert_called_once()
            self.assertEqual(client.connect.call_args.kwargs["banner_timeout"], 30)
            self.assertEqual(client.connect.call_args.kwargs["timeout"], 30)
            self.assertIsNone(session._connect_sock)
            self.assertEqual(session._generation, 0)

    def test_interface_down_then_up_uses_new_socket_and_accepts_connected_exception_fd(self):
        session = SshSession()
        unavailable, connected = Mock(), Mock()
        unavailable.connect_ex.return_value = 10051   # WSAENETUNREACH, unplugged adapter
        unavailable.connect.side_effect = OSError(10051, "network unreachable")
        connected.connect_ex.return_value = 10035     # WSAEWOULDBLOCK
        connected.connect.side_effect = BlockingIOError(10035, "pending")
        connected.getsockopt.return_value = 0
        connected.getpeername.return_value = ("127.0.0.1", 22)
        address = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 22))]
        with patch("devmem_studio.core.socket.getaddrinfo", return_value=address) as resolve, \
                patch("devmem_studio.core.socket.socket", side_effect=[unavailable, connected]), \
                patch("devmem_studio.core.select.select", return_value=([], [], [connected])):
            with self.assertRaises(OSError):
                session._connect_socket_abortably("127.0.0.1", 22, 1, session._generation)
            self.assertIsNone(session._connect_sock)
            self.assertIs(session._connect_socket_abortably("127.0.0.1", 22, 1, session._generation), connected)
        self.assertEqual(resolve.call_count, 2)
        unavailable.close.assert_called_once()
        connected.getpeername.assert_called_once()
        session.close()

    def test_cancel_during_pending_tcp_connect_closes_socket_and_stops_immediately(self):
        session = SshSession()
        sock = Mock()
        sock.connect_ex.return_value = errno.EINPROGRESS
        address = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 22))]

        def cancel_on_wait(*_):
            session.close()
            return [], [], []

        with patch("devmem_studio.core.socket.getaddrinfo", return_value=address), \
                patch("devmem_studio.core.socket.socket", return_value=sock), \
                patch("devmem_studio.core.select.select", side_effect=cancel_on_wait):
            with self.assertRaisesRegex(CommandError, "已取消"):
                session._connect_socket_abortably("127.0.0.1", 22, 30, session._generation)
        self.assertIsNone(session._connect_sock)
        self.assertTrue(sock.close.called)
