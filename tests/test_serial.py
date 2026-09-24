"""Exercise SerialSession against a deterministic in-process fake serial port.

The fake replaces ``serial.serial_for_url`` with a synchronous board emulator
(line-driven console + marker protocol), so there is no TCP/thread timing to
flake: the "board" responds the moment a line completes."""
import re
import unittest
from unittest.mock import patch

from devmem_studio.core import CommandError
from devmem_studio.serial_session import SerialSession

from serial.serialutil import SerialException


class FakeBoard:
    """Line-oriented console the client talks to, streaming UART-style output.

    The console is fully synchronous: whatever the client writes is answered
    before the next read, exactly like a real UART with echo off."""
    def __init__(self, require_login=False):
        self.require_login = require_login
        self.commands = []
        self._login = "login" if require_login else None
        self._last_exit = 0
        self._out = bytearray()
        self.is_open = True
        self.closed = False
        if require_login:
            # A console in login-wait actively broadcasts the prompt until creds.
            self._out += b"board login: "

    def write_line(self, text):
        if self._login == "login":
            # The login: banner was already emitted at construction; a getty
            # prompts for a password once the username has been submitted.
            self._out += b"Password: "
            self._login = "password"
            return
        if self._login == "password":
            self._out += b"\r\nboard# \r\n"
            self._login = None
            return
        if text == "stty -echo":
            return
        if text.startswith("printf ") and "__DM_" in text:
            found = re.search(r"__DM_([0-9a-f]+)_", text)
            if found:
                self._out += ("\r\n__DM_%s_%d__\r\nboard# \r\n"
                              % (found.group(1), self._last_exit)).encode()
            return
        self.commands.append(text)
        self._last_exit = 0
        if text == "fail":
            self._out += b"Error 13: Permission denied\r\n"
            self._last_exit = 7
        else:
            self._out += ("out:" + text + "\r\n").encode()

    # -- client-facing serial API --
    @property
    def in_waiting(self):
        return len(self._out)

    def read(self, size):
        if not self._out:
            return b""
        take = self._out[:size]
        del self._out[:size]
        return bytes(take)

    def write(self, data):
        if not self.is_open:
            raise SerialException("port is not open")
        buf = data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.strip().decode("utf-8", "replace")
            if text:
                self.write_line(text)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self._out.clear()
        self.is_open = False
        self.closed = True


class SerialSessionTests(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.session = SerialSession(lambda level, message: self.logs.append((level, message)))
        self.board = None
        self._patcher = None

    def tearDown(self):
        self.session.close()
        if self._patcher:
            self._patcher.stop()

    def connect(self, require_login=False, username="", password="", timeout=2):
        self.board = FakeBoard(require_login=require_login)
        self._patcher = patch("devmem_studio.serial_session.serial.serial_for_url",
                              lambda *a, **k: self.board)
        self._patcher.start()
        self.session.connect("fake://port", 115200, username, password, timeout=timeout)
        return self.board

    def test_connect_auto_login_and_run(self):
        board = self.connect()
        self.assertTrue(self.session.alive)
        self.assertEqual(self.session.run("pwd", timeout=2), "out:pwd")
        self.assertEqual(board.commands, ["pwd"])
        self.assertEqual(self.session.last_exit, 0)

    def test_login_prompt_without_password_times_out(self):
        """A console that shows login: but never completes the handshake must
        fail with a timeout instead of half-connecting as a silent shell."""
        class StuckLogin(FakeBoard):
            def write_line(self, text):
                if self._login == "login":
                    self._login = "password"   # consume the username, emit nothing
                    return
                return super().write_line(text)

        board = StuckLogin(require_login=True)
        self._patcher = patch("devmem_studio.serial_session.serial.serial_for_url",
                              lambda *a, **k: board)
        self._patcher.start()
        with self.assertRaisesRegex(CommandError, "串口登录超时"):
            self.session.connect("fake://port", 115200, "root", "secret", timeout=0.5)
        self.assertFalse(self.session.alive)

    def test_run_captures_exit_code_and_raises_on_failure(self):
        self.connect()
        with self.assertRaisesRegex(CommandError, "退出码 7"):
            self.session.run("fail", timeout=2)
        self.assertEqual(self.session.last_exit, 7)
        self.assertTrue(self.session.alive)  # a failed command does not drop the session

    def test_login_prompt_feeds_credentials(self):
        board = self.connect(require_login=True, username="root", password="secret")
        self.assertTrue(self.session.alive)
        self.assertEqual(self.session.run("whoami", timeout=2), "out:whoami")
        # board saw the login handshake (username -> Password: -> password)
        self.assertEqual(board.commands, ["whoami"])

    def test_no_login_prompt_is_root_shell(self):
        board = self.connect()
        self.assertEqual(self.session.run("pwd", timeout=2), "out:pwd")
        self.assertFalse(board.require_login)

    def test_delayed_banner_still_detects_login(self):
        """A transport that surfaces the boot banner a few reads late must not
        miss the login handshake. The fake hides the prompt until read N."""
        board = self.connect(require_login=True, username="root", password="secret")

        class Delayed(FakeBoard):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._reads = 0
                self._delay = 3
                self._banner_sent = False

            @property
            def in_waiting(self):
                self._reads += 1
                if self._reads <= self._delay:
                    return 0
                return super().in_waiting

            def read(self, size):
                if self._reads <= self._delay:
                    self._reads += 1
                    return b""
                return super().read(size)

        delayed = Delayed(require_login=True)
        self._patcher.stop()
        self._patcher = patch("devmem_studio.serial_session.serial.serial_for_url",
                              lambda *a, **k: delayed)
        self._patcher.start()
        self.session.close()
        self.session.connect("fake://port", 115200, "root", "secret", timeout=2)
        self.assertTrue(self.session.alive)
        self.assertEqual(self.session.run("whoami", timeout=2), "out:whoami")
        self.assertEqual(delayed.commands, ["whoami"])

    def test_run_before_connect_raises(self):
        with self.assertRaisesRegex(CommandError, "串口未连接"):
            self.session.run("echo hi", timeout=0.5)

    def test_file_transfer_methods_refused(self):
        self.connect()
        for name in ("upload_bitfile", "list_bit_backups", "rollback_bit", "download_file"):
            self.assertFalse(hasattr(self.session, name), f"serial must not expose {name}")


if __name__ == "__main__":
    unittest.main()