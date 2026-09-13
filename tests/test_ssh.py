"""Exercise real Paramiko encryption/auth/channels against a local disposable board simulator."""
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import paramiko
from devmem_studio.catalog import DEFAULT_TYPES
from devmem_studio.core import SshSession, CommandError, HostKeyChangedError, DEFAULT_LOG_PATH


class BoardServer(paramiko.ServerInterface):
    def __init__(self):
        self.memory = {}
        self.commands = []
        self.exec_commands = []
        self.auth_attempts = 0

    def check_auth_password(self, username, password):
        self.auth_attempts += 1
        return paramiko.AUTH_SUCCESSFUL if (username, password) == ("test", "test") else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=self.shell, args=(channel,), daemon=True).start()
        return True

    def shell(self, channel):
        buffer = b""
        last_exit = 0
        hanging = False
        try:
            while not channel.closed:
                chunk = channel.recv(32768)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode().strip()
                    if text == "stty -echo":
                        channel.sendall(b"board# ")
                        continue
                    if hanging:
                        continue
                    if text.startswith("printf "):
                        marker = re.search(r"__DM_[a-f0-9]+_", text).group(0)
                        channel.sendall(f"\r\n{marker}{last_exit}__\r\nboard# ".encode())
                        continue
                    self.commands.append(text)
                    last_exit = 0
                    if text == "hang":
                        hanging = True
                    elif text == "failure":
                        channel.sendall(b"Error 13: Permission denied\r\n")
                        last_exit = 7
                    elif text.startswith("devmem "):
                        parts = text.split()
                        address = int(parts[1], 0)
                        if len(parts) == 4:
                            self.memory[address] = int(parts[3], 0)
                        else:
                            channel.sendall(f"0x{self.memory.get(address, 0x2A):08X}\r\n".encode())
                    elif text == "pwd":
                        channel.sendall(b"/root\r\n")
        except (EOFError, OSError):
            pass

    def check_channel_exec_request(self, channel, command):
        self.exec_commands.append(command.decode())
        def stream():
            try:
                if command == b"tail -f /tmp/missing.log":
                    time.sleep(0.05)
                    channel.send_stderr(b"tail: /tmp/missing.log: No such file or directory\n")
                    channel.send_exit_status(1)
                    channel.close()
                    return
                # Deliberately split a Chinese UTF-8 character across SSH packets.
                payload = "第一条日志\n".encode()
                channel.sendall(payload[:2])
                time.sleep(0.05)
                channel.sendall(payload[2:])
                while not channel.closed:
                    time.sleep(0.03)
            except (EOFError, OSError):
                pass
        threading.Thread(target=stream, daemon=True).start()
        return True


class BoardFixture:
    host_key_types = None
    kex_types = None
    connect_on_setup = True

    @classmethod
    def setUpClass(cls):
        cls.key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.server = BoardServer()
        self.transport = None
        self.server_errors = []
        self.channels = []
        self.stop = threading.Event()
        def accept():
            while not self.stop.is_set():
                transport = None
                try:
                    client, _ = self.listener.accept()
                    self.transport = transport = paramiko.Transport(client)
                    options = transport.get_security_options()
                    if self.host_key_types:
                        options.key_types = self.host_key_types
                    if self.kex_types:
                        options.kex = self.kex_types
                    transport.add_server_key(self.key)
                    transport.start_server(server=self.server)
                    while not self.stop.is_set() and transport.is_active():
                        channel = transport.accept(timeout=0.1)
                        if channel:
                            self.channels.append(channel)
                except socket.timeout:
                    continue
                except (OSError, EOFError, paramiko.SSHException) as exc:
                    self.server_errors.append(exc)
                finally:
                    if transport:
                        transport.close()
        self.server_thread = threading.Thread(target=accept, daemon=True)
        self.server_thread.start()
        self.logs = []
        self.session = SshSession(lambda level, message: self.logs.append((level, message)), Path(self.temp.name))
        self.addCleanup(self.close_board)
        if self.connect_on_setup:
            self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)

    def close_board(self):
        self.session.close()
        self.stop.set()
        if self.transport:
            self.transport.close()
        self.listener.close()
        self.server_thread.join(1)
        self.temp.cleanup()


class SshTests(BoardFixture, unittest.TestCase):
    def test_basic_component_registers_read_write_over_real_ssh(self):
        for index, reg in enumerate(DEFAULT_TYPES["basic"]["registers"]):
            address = 0xB0102200 + int(reg["offset"], 16)
            value = 0x12340000 + index
            with self.subTest(name=reg["name"]):
                self.assertEqual(self.session.write(address, 32, value), value)
                self.assertEqual(self.server.memory[address], value)
                self.assertIn(f"devmem 0x{address:08x} 32 0x{value:x}", self.server.commands)

    def test_real_ssh_read_write_and_exit_codes(self):
        self.assertTrue(self.session.alive)
        self.assertEqual(self.session.client.get_transport().host_key_type, "rsa-sha2-512")
        self.assertEqual(self.session.read(0xB0102208), 42)
        self.assertEqual(self.session.write(0xB0102208, 32, 123), 123)
        self.assertEqual(self.session.run("pwd"), "/root")
        with self.assertRaisesRegex(CommandError, "退出码 7"):
            self.session.run("failure")
        self.assertEqual(self.session.last_exit, 7)
        self.assertEqual(self.session.read(0xB0102208), 123)
        self.assertTrue((Path(self.temp.name) / "known_hosts").read_text().strip())

    def test_stream_has_own_channel_and_utf8_decoder(self):
        output = []
        stopped = threading.Event()
        self.session.start_stream(DEFAULT_LOG_PATH, output.append, stopped.set)
        self.assertEqual(self.server.exec_commands, ["tail -f /run/media/sda/sunny.log"])
        self.assertIsNot(self.session._stream_channel, self.session.chan)
        self.assertFalse(any(command.startswith("tail ") for command in self.server.commands))
        self.assertEqual(self.session.read(0xB0102208), 42)
        self.assertEqual(self.session.write(0xB0102208, 32, 321), 321)
        deadline = time.monotonic() + 2
        while "第一条日志" not in "".join(output) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("第一条日志", "".join(output))
        self.assertNotIn("�", "".join(output))
        self.session.stop_stream()
        self.assertTrue(stopped.wait(1))
        self.assertTrue(self.session.alive)
        self.assertEqual(self.session.read(0xB0102208), 321)

    def test_missing_log_file_does_not_close_register_shell(self):
        output, stopped = [], threading.Event()
        self.session.start_stream("/tmp/missing.log", output.append, stopped.set)
        self.assertTrue(stopped.wait(2))
        self.assertIn("No such file", "".join(output))
        self.assertIn("状态码 1", "".join(output))
        self.assertTrue(self.session.alive)
        self.assertEqual(self.session.read(0xB0102208), 42)
        self.assertEqual(self.session.write(0xB0102208, 32, 123), 123)

    def test_timeout_invalidates_shell_before_next_command(self):
        with self.assertRaises(TimeoutError):
            self.session.run("hang", timeout=0.15)
        self.assertFalse(self.session.alive)
        with self.assertRaises(CommandError):
            self.session.read(0xB0102208)

    def test_disconnect_is_detected(self):
        self.transport.close()
        deadline = time.monotonic() + 1
        while self.session.alive and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.session.alive)
        with self.assertRaises(CommandError):
            self.session.read(0xB0102208)


class LegacySshTests(BoardFixture, unittest.TestCase):
    host_key_types = ("ssh-rsa",)
    kex_types = ("diffie-hellman-group14-sha1",)
    connect_on_setup = False

    def test_legacy_rsa_handshake_read_write_and_stream(self):
        self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)
        self.assertEqual(self.session.client.get_transport().host_key_type, "ssh-rsa")
        self.assertTrue(any("旧设备兼容" in message for _, message in self.logs))
        self.assertEqual(self.session.read(0xB0102208), 42)
        self.assertEqual(self.session.write(0xB0102208, 32, 123), 123)
        output, stopped = [], threading.Event()
        self.session.start_stream("/tmp/legacy.log", output.append, stopped.set)
        deadline = time.monotonic() + 2
        while "第一条日志" not in "".join(output) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("第一条日志", "".join(output))
        self.assertEqual(self.session.read(0xB0102208), 123)
        self.session.stop_stream()
        self.assertTrue(stopped.wait(1))

    def test_removing_legacy_host_algorithm_reproduces_reported_error(self):
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        with self.assertRaisesRegex(paramiko.ssh_exception.IncompatiblePeer, "no acceptable host key"):
            client.connect("127.0.0.1", port=self.port, username="test", password="test", timeout=2,
                           allow_agent=False, look_for_keys=False, disabled_algorithms={"keys": ["ssh-rsa"]})
        self.assertEqual(self.server.auth_attempts, 0)


class HostKeyTests(BoardFixture, unittest.TestCase):
    connect_on_setup = False

    def save_host_key(self, key):
        keys = paramiko.HostKeys()
        keys.add(f"[127.0.0.1]:{self.port}", "ssh-rsa", key)
        keys.save(str(Path(self.temp.name) / "known_hosts"))

    def test_saved_rsa_key_still_prefers_sha2(self):
        self.save_host_key(self.key)
        self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)
        self.assertEqual(self.session.client.get_transport().host_key_type, "rsa-sha2-512")
        self.assertEqual(self.session.read(0xB0102208), 42)

    def test_changed_host_key_is_rejected_before_authentication(self):
        self.save_host_key(paramiko.RSAKey.generate(2048))
        with self.assertRaises(HostKeyChangedError) as captured:
            self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)
        self.assertIsInstance(captured.exception, paramiko.BadHostKeyException)
        self.assertEqual(captured.exception.key, self.key)
        self.assertEqual(captured.exception.hostkey_name, f"[127.0.0.1]:{self.port}")
        self.assertTrue(captured.exception.new_fingerprint.startswith("SHA256:"))
        self.assertFalse(self.session.alive)
        self.assertEqual(self.server.auth_attempts, 0)

    def test_approved_key_recovers_connection_and_preserves_other_hosts(self):
        old = paramiko.RSAKey.generate(2048)
        path = Path(self.temp.name) / "known_hosts"
        target = f"[127.0.0.1]:{self.port}"
        original = ("# saved board identities\r\n"
                    f"{target},other-board ssh-rsa {old.get_base64()} shared entry\r\n"
                    f"unrelated-board ssh-rsa {self.key.get_base64()}\r\n").encode()
        path.write_bytes(original)
        with self.assertRaises(HostKeyChangedError) as captured:
            self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)
        self.assertEqual(self.server.auth_attempts, 0)
        change = captured.exception
        self.assertEqual(change.expected_key, old)
        self.assertNotEqual(change.old_fingerprint, change.new_fingerprint)
        backup = self.session.replace_host_key(change)
        self.assertEqual(backup.read_bytes(), original)
        keys = paramiko.HostKeys(str(path))
        self.assertTrue(keys.check(target, self.key))
        self.assertTrue(keys.check("other-board", old))
        self.assertTrue(keys.check("unrelated-board", self.key))
        self.assertIn(f"other-board ssh-rsa {old.get_base64()} shared entry\r\n".encode(), path.read_bytes())
        self.assertTrue(path.read_bytes().startswith(b"# saved board identities\r\n"))
        updated = path.read_bytes()
        self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)
        self.assertEqual(self.session.write(0xB0102208, 32, 321), 321)
        self.session.close()
        self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)
        self.assertEqual(self.session.read(0xB0102208), 321)
        self.assertEqual(path.read_bytes(), updated)
        self.assertEqual(self.server.auth_attempts, 2)

    def test_cache_changed_during_review_is_not_overwritten(self):
        self.save_host_key(paramiko.RSAKey.generate(2048))
        with self.assertRaises(HostKeyChangedError) as captured:
            self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)
        self.save_host_key(paramiko.RSAKey.generate(2048))
        path = Path(self.temp.name) / "known_hosts"
        modified = path.read_bytes()
        with self.assertRaisesRegex(CommandError, "记录已变化"):
            self.session.replace_host_key(captured.exception)
        self.assertEqual(path.read_bytes(), modified)
        self.assertEqual(list(path.parent.glob("known_hosts.backup-*")), [])
        self.assertEqual(self.server.auth_attempts, 0)

    def test_retry_rejects_another_key_change_before_authentication(self):
        self.save_host_key(paramiko.RSAKey.generate(2048))
        with self.assertRaises(HostKeyChangedError) as first:
            self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)
        approved = first.exception
        self.session.replace_host_key(approved)
        self.key = paramiko.RSAKey.generate(2048)
        with self.assertRaises(HostKeyChangedError) as second:
            self.session.connect("127.0.0.1", self.port, "test", "test", timeout=2)
        self.assertEqual(second.exception.expected_key, approved.key)
        self.assertEqual(second.exception.key, self.key)
        self.assertEqual(self.server.auth_attempts, 0)
        self.assertFalse(self.session.alive)

    def test_default_port_hashed_host_update_preserves_unrelated_hash(self):
        old = paramiko.RSAKey.generate(2048)
        host = "192.0.2.10"
        change = HostKeyChangedError(host, 22, self.key, old)
        hashed = paramiko.HostKeys.hash_host(host)
        other = paramiko.HostKeys.hash_host("192.0.2.11")
        path = Path(self.temp.name) / "known_hosts"
        original = f"{hashed} ssh-rsa {old.get_base64()}\n{other} ssh-rsa {old.get_base64()}\n".encode()
        path.write_bytes(original)
        backup = self.session.replace_host_key(change)
        self.assertEqual(backup.read_bytes(), original)
        self.assertNotIn(hashed.encode(), path.read_bytes())
        self.assertIn(other.encode(), path.read_bytes())
        keys = paramiko.HostKeys(str(path))
        self.assertTrue(keys.check(host, self.key))
        self.assertTrue(keys.check("192.0.2.11", old))


if __name__ == "__main__":
    unittest.main()
