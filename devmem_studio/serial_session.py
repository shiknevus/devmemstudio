# -*- coding: utf-8 -*-
"""Serial console shell transport; a parallel connection alongside SSH."""
from __future__ import annotations

import re
import threading
import time
import uuid

import serial

from .core import CommandError, strip_ansi, _strip_shell_prompt

# Same marker protocol as SshSession.run: shell echoes ``__DM_<hex>_<exit>__``.
# A console prompt may carry a host prefix ("board login: "), so match anywhere.
_LOGIN_RE = re.compile(r"[Ll]ogin:")
_PASSWORD_RE = re.compile(r"[Pp]assword:")

# Short per-read timeout so polling loops stay snappy regardless of the
# handshake timeout; command-level timeouts are managed by deadline loops.
_READ_TIMEOUT = 0.4


class SerialSession:
    """Interactive console shell over a physical or emulated serial port.

    Carries the same devmem marker protocol as SSH so a command line works on
    either transport, but intentionally has no file-transfer or log-stream
    capability: bit upload/rollback and sunny.log stay on SSH."""
    def __init__(self, log=lambda level, text: None):
        self.log = log
        self.ser = None
        self.last_exit = None
        self._lock = threading.Lock()
        self._closed = False
        self._cancel = threading.Event()

    def cancel_connect(self):
        """Abort an in-flight connect(): the handshake loops raise immediately."""
        self._cancel.set()

    @property
    def alive(self):
        try:
            return bool(self.ser and self.ser.is_open and not self._closed)
        except Exception:
            return False

    def connect(self, port, baud=115200, username="", password="", timeout=8):
        self.close()
        # _cancel is not re-armed here: a cancel issued before the worker reaches
        # connect() must still win. The window uses a fresh session per attempt.
        ser =serial.serial_for_url(port, baudrate=baud, timeout=_READ_TIMEOUT, write_timeout=_READ_TIMEOUT)
        self.ser = ser
        self._closed = False
        self.last_exit = None
        try:
            self.log("SYSTEM", f"串口已打开：{port} @ {baud} bps。")
            # A console may sit at login:/Password: or already be a root shell.
            self._auto_login(username, password, timeout)
            self._send(b"stty -echo\n")
            self._drain(0.15)
            self.log("SYSTEM", "串口控制台已就绪。")
        except Exception:
            self.close()
            raise

    def _send(self, data: bytes):
        ser = self.ser   # close() from the GUI thread may null it mid-command
        try:
            if ser is None:
                raise serial.SerialException("port closed")
            ser.write(data)
            ser.flush()
        except (serial.SerialException, OSError, AttributeError):
            self._closed = True
            raise CommandError("串口连接已断开，请重新连接。") from None

    def _read_raw(self) -> bytes:
        """Read whatever is buffered; raises CommandError on a dead port.

        Idle UARTs return immediately instead of blocking on ``read(1)`` (which
        would stall every quiet poll for the full serial read timeout). Callers
        loop until their own deadline, so a short sleep between passes is all
        the idle cost."""
        ser = self.ser
        try:
            if ser is None:
                raise serial.SerialException("port closed")
            waiting = ser.in_waiting
            return ser.read(waiting) if waiting else b""
        except (serial.SerialException, OSError, AttributeError):
            self._closed = True
            raise CommandError("串口连接已断开，请重新连接。") from None

    def _recv_text(self, seconds: float, settled=True) -> str:
        """Collect input for up to `seconds`.

        With ``settled=True`` a quiet line (0.15s with data already buffered)
        ends the read early; ``settled=False`` always waits the full window,
        which the login handshake needs because prompts arrive in discrete
        chunks with gaps between them."""
        data = bytearray()
        deadline = time.monotonic() + seconds
        idle_since = time.monotonic()
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                raise CommandError("连接已取消。")
            try:
                chunk = self._read_raw()
            except CommandError:
                raise
            if chunk:
                data += chunk
                idle_since = time.monotonic()
            elif settled and data and time.monotonic() - idle_since >= 0.15:
                break
            time.sleep(0.01)
        return strip_ansi(data.decode("utf-8", "replace"))

    def _drain(self, seconds: float):
        self._recv_text(seconds)

    def _auto_login(self, username, password, timeout):
        """Detect login:/Password: prompts and feed credentials when present.

        Without prompts the console is treated as an already-logged-in root
        shell. The handshake stays snappy on quiet consoles: data arrival breaks
        the wait immediately, and an empty line only needs a short settle before
        concluding it is a root shell."""
        # First beat: a console that broadcasts a login:/banner answers fast, so
        # settle early on data; a truly quiet UART returns in the short window.
        text = self._recv_text(min(timeout, 0.5))
        if _LOGIN_RE.search(text):
            self._send((username + "\n").encode())
            # The password phase honours the connect timeout: a slow board may
            # take seconds before asking for the password, and a console that
            # never completes the handshake must fail instead of half-connect.
            deadline = time.monotonic() + timeout
            buffer = bytearray(text.encode("utf-8", "replace"))
            while time.monotonic() < deadline:
                if self._cancel.is_set():
                    raise CommandError("连接已取消。")
                decoded = strip_ansi(buffer.decode("utf-8", "replace"))
                if _PASSWORD_RE.search(decoded):
                    self._send((password + "\n").encode())
                    self._recv_text(0.3)  # swallow the post-login banner/prompt
                    return
                if re.search(r"[#$>]\s*$", decoded) and "\n" in decoded:
                    return  # shell prompt behind us: login completed without password
                try:
                    chunk = self._read_raw()
                except CommandError:
                    return
                if chunk:
                    buffer += chunk
                time.sleep(0.01)
            raise CommandError(f"串口登录超时（{timeout:g} 秒未完成握手），请检查控制台状态。")
        # No login prompt: an async transport may still be surfacing the banner.
        if not text.strip() and not re.search(r"[#$>]\s", text):
            text = self._recv_text(0.2, settled=False)
        if not _LOGIN_RE.search(text):
            return  # quiet console: treat as an already-logged-in root shell

    def run(self, command: str, timeout=8, quiet=False):
        if not command.strip() or "\x00" in command:
            raise ValueError("命令不能为空或包含空字符。")
        with self._lock:
            if not self.alive:
                raise CommandError("串口未连接，请先连接串口。")
            try:
                drain_until = time.monotonic() + 0.2   # bounded: a chatty console never goes quiet
                while self._read_raw() and time.monotonic() < drain_until:  # drop stale printk/echoes
                    pass
            except CommandError:
                self.close()
                raise
            if not quiet:
                self.log("CMD", command)
            marker = "__DM_" + uuid.uuid4().hex[:16] + "_"
            end_re = re.compile(r"(?:^|\n)" + re.escape(marker) + r"(\d+)__\s*(?:\n|$)")
            full = command.rstrip() + "\nprintf '\\n" + marker + "%s__\\n' \"$?\"\n"
            self._send(full.encode("utf-8"))
            output = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    chunk = self._read_raw()
                except CommandError:
                    self.close()
                    raise
                if chunk:
                    output.extend(chunk)
                    if len(output) > 4 * 1024 * 1024:
                        self.close()
                        raise CommandError("命令输出超过 4 MB，已断开连接。")
                    decoded = strip_ansi(output.decode("utf-8", "replace"))
                    match = end_re.search(decoded)
                    if match:
                        self.last_exit = int(match.group(1))
                        lines = []
                        for line in decoded[:match.start()].splitlines():
                            line = _strip_shell_prompt(line)
                            if (not line.strip() or marker in line or line.strip() == command.strip()
                                    or re.fullmatch(r"[^\n]*[#$>]\s*", line)):
                                continue
                            lines.append(line)
                        result = "\n".join(lines).strip()
                        if result and not quiet:
                            self.log("INFO" if self.last_exit == 0 else "ERROR", result)
                        if self.last_exit:
                            raise CommandError(f"命令退出码 {self.last_exit}：{result or command}")
                        return result
                time.sleep(0.01)
            # A console without its terminator is not safe to reuse.
            self.close()
            raise TimeoutError(f"命令超过 {timeout:g} 秒未完成，连接已关闭，请重新连接。")

    def close(self):
        self._closed = True
        ser, self.ser = self.ser, None
        if ser:
            try:
                ser.close()
            except Exception:
                pass


def list_serial_ports():
    """[(device, description)] for attached COM ports, e.g. ('COM5', 'USB-SERIAL CH340').

    ``device`` is the connectable value; ``description`` is the human-readable
    board name shown next to it in the picker."""
    try:
        from serial.tools import list_ports
        return [(port.device, (port.description or "").strip()) for port in list_ports.comports()]
    except Exception:
        return []