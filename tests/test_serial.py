"""SerialSession against an in-process fake port: streaming, keystrokes, unplug,
auto-login and the U-Boot catch. The fake board answers synchronously inside
write(), so only the pump thread's own polling adds latency."""
import threading
import time
import unittest
from unittest.mock import patch

from serial.serialutil import SerialException

from devmem_studio.core import CommandError
from devmem_studio.serial_session import SerialSession


class FakePort:
    """Thread-safe UART stand-in; `script(written_bytes)` lets a test play the board."""
    def __init__(self, script=None):
        self.script = script or (lambda port, data: None)
        self.written = bytearray()
        self._out = bytearray()
        self._lock = threading.Lock()
        self.is_open = True
        self.unplugged = False

    def emit(self, text):
        with self._lock:
            self._out += text.encode("utf-8") if isinstance(text, str) else text

    def unplug(self):
        self.unplugged = True

    @property
    def in_waiting(self):
        if self.unplugged:
            raise SerialException("ClearCommError failed (PermissionError(13, 'device gone'))")
        with self._lock:
            return len(self._out)

    def read(self, size):
        with self._lock:
            data = bytes(self._out[:size])
            del self._out[:size]
            return data

    def write(self, data):
        if not self.is_open or self.unplugged:
            raise SerialException("WriteFile failed")
        self.written += data
        self.script(self, bytes(data))
        return len(data)

    def close(self):
        self.is_open = False


def wait_for(condition, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class SerialSessionTests(unittest.TestCase):
    def setUp(self):
        self.logs, self.data, self.lost, self.uboot = [], [], [], []
        self.session = SerialSession(lambda level, text: self.logs.append((level, text)),
                                     on_data=self.data.append, on_lost=self.lost.append,
                                     on_uboot=self.uboot.append)
        self._patcher = None

    def tearDown(self):
        self.session.close()
        if self._patcher:
            self._patcher.stop()

    def connect(self, port, username="", password=""):
        self._patcher = patch("devmem_studio.serial_session.serial.serial_for_url", lambda *a, **k: port)
        self._patcher.start()
        self.session.connect("fake://port", 115200, username, password)
        return port

    def received(self):
        return "".join(self.data)

    def test_connect_sends_nothing_and_streams_unsolicited_output(self):
        port = self.connect(FakePort())
        port.emit("[  12.5] usb 1-1: new device\r\n")
        port.emit("Hit any key to stop autoboot:  4 ")
        self.assertTrue(wait_for(lambda: "autoboot" in self.received()))
        self.assertEqual(bytes(port.written), b"")   # a connect must never stop autoboot by itself
        self.assertTrue(self.session.alive)

    def test_keystrokes_go_out_in_order(self):
        port = self.connect(FakePort())
        for key in (b"l", b"s", b"\t", b"\x1b[A", b"\r", b"&"):
            self.session.send(key)
        self.assertTrue(wait_for(lambda: bytes(port.written) == b"ls\t\x1b[A\r&"))

    def test_split_utf8_is_decoded_across_reads(self):
        port = self.connect(FakePort())
        encoded = "中文".encode("utf-8")
        port.emit(encoded[:2])
        time.sleep(0.05)
        port.emit(encoded[2:])
        self.assertTrue(wait_for(lambda: self.received() == "中文"))

    def test_unplug_reports_lost_once_and_closes(self):
        port = self.connect(FakePort())
        port.unplug()
        self.assertTrue(wait_for(lambda: self.lost))
        time.sleep(0.05)
        self.assertEqual(len(self.lost), 1)
        self.assertIn("已断开", self.lost[0])
        self.assertFalse(self.session.alive)
        with self.assertRaises(CommandError):
            self.session.send(b"x")

    def test_intentional_close_is_not_reported_as_lost(self):
        self.connect(FakePort())
        self.session.close()
        time.sleep(0.05)
        self.assertEqual(self.lost, [])
        self.assertFalse(self.session.alive)

    def test_auto_login_answers_getty_with_saved_credentials(self):
        def board(port, data):
            if data == b"root\r":
                port.emit("root\r\nPassword: ")
            elif data == b"secret\r":
                port.emit("\r\nroot@board:~# ")
        port = self.connect(FakePort(board), "root", "secret")
        port.emit("\r\nboard login: ")
        self.assertTrue(wait_for(lambda: ("SYSTEM", "串口已自动登录。") in self.logs))
        self.assertEqual(bytes(port.written), b"root\rsecret\r")

    def test_wrong_password_stops_auto_login(self):
        def board(port, data):
            if data == b"root\r":
                port.emit("root\r\nPassword: ")
            elif data == b"bad\r":
                port.emit("\r\nLogin incorrect\r\nboard login: ")
        port = self.connect(FakePort(board), "root", "bad")
        port.emit("board login: ")
        self.assertTrue(wait_for(lambda: any(level == "ERROR" for level, _ in self.logs)))
        time.sleep(0.5)
        self.assertEqual(bytes(port.written), b"root\rbad\r")   # no endless retry
        self.assertIn("用户名或密码错误", self.logs[-1][1])

    def test_unrelated_password_prompt_is_never_answered(self):
        port = self.connect(FakePort(), "root", "secret")
        port.emit("root@board:~# passwd\r\nNew password: ")
        time.sleep(0.5)
        self.assertEqual(bytes(port.written), b"")

    def test_login_prompt_without_username_is_left_to_the_user(self):
        port = self.connect(FakePort())
        port.emit("board login: ")
        time.sleep(0.5)
        self.assertEqual(bytes(port.written), b"")

    def test_uboot_catch_holds_key_until_prompt(self):
        def board(port, data):
            if data == b"&" and port.written.count(b"&") == 3:   # this U-Boot ignores a lone tap
                port.emit("\x08\x08\x08 0 \r\nZynqMP> ")
        port = self.connect(FakePort(board))
        self.session.arm_uboot_stop(b"&")
        port.emit("U-Boot 2018.01\r\n")
        time.sleep(0.1)
        self.assertEqual(bytes(port.written), b"")   # nothing before the countdown
        port.emit("Hit any key to stop autoboot:  4 ")
        self.assertTrue(wait_for(lambda: self.uboot == [True]))
        self.assertEqual(bytes(port.written), b"&&&\x03")
        self.assertFalse(self.session.uboot_armed)

    def test_uboot_catch_gives_up_once_the_kernel_starts(self):
        port = self.connect(FakePort())
        self.session.arm_uboot_stop(b"&")
        port.emit("Hit any key to stop autoboot:  4 ")
        self.assertTrue(wait_for(lambda: b"&" in port.written))
        port.emit("\x08\x08\x08 0 \r\nStarting kernel ...\r\n")
        self.assertTrue(wait_for(lambda: self.uboot == [False]))
        sent = len(port.written)
        time.sleep(0.15)
        self.assertEqual(len(port.written), sent)

    def test_busy_or_missing_port_gives_actionable_message(self):
        for error, hint in (("could not open port 'COM35': PermissionError(13, '拒绝访问。', None, 5)", "被占用"),
                            ("could not open port 'COM99': FileNotFoundError(2, '系统找不到指定的文件。')", "找不到串口")):
            def refuse(*args, **kwargs):
                raise SerialException(error)
            with patch("devmem_studio.serial_session.serial.serial_for_url", refuse):
                with self.assertRaises(CommandError) as raised:
                    self.session.connect("COM35", 115200)
            self.assertIn(hint, str(raised.exception))

    def test_cancel_before_connect_wins(self):
        self.session.cancel_connect()
        with patch("devmem_studio.serial_session.serial.serial_for_url", lambda *a, **k: FakePort()):
            with self.assertRaises(CommandError) as raised:
                self.session.connect("COM35", 115200)
        self.assertIn("已取消", str(raised.exception))

    def test_send_before_connect_raises(self):
        with self.assertRaises(CommandError):
            self.session.send(b"x")

    def test_no_command_protocol_or_file_transfer(self):
        for name in ("run", "upload_bitfile", "download_file", "list_bit_backups", "rollback_bit", "start_stream"):
            self.assertFalse(hasattr(self.session, name), name)


if __name__ == "__main__":
    unittest.main()
