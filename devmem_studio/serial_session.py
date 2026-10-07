# -*- coding: utf-8 -*-
"""Serial console transport: a raw byte stream beside SSH, pumped on its own thread."""
from __future__ import annotations

import codecs
import queue
import re
import threading
import time

import serial

from .core import CommandError, strip_ansi

# A console prompt may carry a host prefix ("board login: ").
_LOGIN_PROMPT = re.compile(r"[Ll]ogin:\s*$")
_PASSWORD_PROMPT = re.compile(r"(?:^|\n)\s*[Pp]assword:\s*$")
_LOGIN_FAILED = re.compile(r"[Ll]ogin incorrect")
_SHELL_PROMPT = re.compile(r"[#$>]\s*$")
_IDLE = 0.01           # pump wait between polls; a queued keystroke wakes it at once
_PROMPT_SETTLE = 0.3   # a login prompt must sit quiet this long before it is answered
_LOGIN_ATTEMPTS = 2
_AUTOBOOT = re.compile(r"autoboot|any key", re.I)
_UBOOT_PROMPT = re.compile(r"(?:^|\n)[\w-]*(?:=>|>) ?$")
_KERNEL_STARTED = re.compile(r"Starting kernel|\[\s*\d+\.\d+\]")
_UBOOT_REPEAT = 0.04   # like a held key: one byte every 40 ms
_UBOOT_HOLD = 6.0      # give up holding once the countdown has surely run out


def _open_error(port, exc):
    text = str(exc)
    if "PermissionError" in text or "拒绝访问" in text or "Access is denied" in text:
        return f"串口 {port} 被占用：请先关闭占用它的串口工具（如 MobaXterm / SecureCRT）后重试。"
    if "FileNotFoundError" in text or "找不到" in text or "cannot find" in text:
        return f"找不到串口 {port}：请检查 USB 串口线是否插好。"
    return f"打开串口 {port} 失败：{text}"


class SerialSession:
    """Interactive serial console.

    Everything the board prints reaches ``on_data`` as text the moment it
    arrives; keystrokes go out through send(). A dead port (USB unplugged)
    is reported once through ``on_lost``. There is deliberately no command
    protocol and no file transfer: bit upload/rollback and sunny.log stay on SSH."""

    def __init__(self, log=lambda level, text: None, on_data=None, on_lost=None, on_uboot=None):
        self.log = log
        self.on_data = on_data or (lambda text: None)
        self.on_lost = on_lost or (lambda message: None)
        self.on_uboot = on_uboot or (lambda caught: None)
        self._uboot = None
        self.ser = None
        self.port = ""
        self._closed = True
        self._generation = 0
        self._cancel = threading.Event()
        self._outbox = queue.Queue()
        self._username = ""
        self._password = ""
        self._auto_login = False
        self._login_stage = None
        self._login_attempts = 0
        self._tail = ""
        self._tail_at = 0.0
        self._tail_dirty = False

    def cancel_connect(self):
        """Abort an in-flight connect()."""
        self._cancel.set()

    @property
    def alive(self):
        try:
            return bool(self.ser and self.ser.is_open and not self._closed)
        except Exception:
            return False

    def connect(self, port, baud=115200, username="", password="", timeout=8):
        """Open the port and start streaming. Nothing is sent: a keystroke now
        could stop a U-Boot countdown the user did not mean to interrupt."""
        self.close()
        # _cancel is not re-armed here: a cancel issued before the worker reaches
        # connect() must still win. The window uses a fresh session per attempt.
        if self._cancel.is_set():
            raise CommandError("连接已取消。")
        try:
            ser = serial.serial_for_url(port, baudrate=baud, timeout=0, write_timeout=2)
        except (serial.SerialException, OSError, ValueError) as exc:
            raise CommandError(_open_error(port, exc)) from None
        if self._cancel.is_set():
            ser.close()
            raise CommandError("连接已取消。")
        self._generation += 1
        self.ser = ser
        self.port = port
        self._closed = False
        self._outbox = queue.Queue()
        self._username, self._password = username, password
        self._auto_login = bool(username)
        self._login_stage = None
        self._login_attempts = 0
        self._tail, self._tail_dirty = "", False
        threading.Thread(target=self._pump, args=(ser, self._generation, self._outbox),
                         name=f"serial-{port}", daemon=True).start()
        self.log("SYSTEM", f"串口已打开：{port} @ {baud} bps。")

    def send(self, data):
        """Queue raw bytes (keystrokes, pasted text) for the board."""
        if not self.alive:
            raise CommandError("串口未连接，请先连接串口。")
        if data:
            self._outbox.put(bytes(data))

    def arm_uboot_stop(self, key=b"&", timeout=300.0):
        """Catch the next autoboot countdown by holding `key` until the U-Boot prompt shows.

        Some boards ignore a single key press there and need it held down."""
        self._uboot = {"key": bytes(key), "until": time.monotonic() + timeout, "tail": "",
                       "since": None, "next": 0.0}

    def disarm_uboot_stop(self):
        self._uboot = None

    @property
    def uboot_armed(self):
        return self._uboot is not None

    def close(self):
        self._closed = True
        self._generation += 1
        ser, self.ser = self.ser, None
        if ser:
            try:
                ser.close()
            except Exception:
                pass

    # -- pump thread ---------------------------------------------------------
    def _pump(self, ser, generation, outbox):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while generation == self._generation:
                while True:
                    try:
                        ser.write(outbox.get_nowait())
                    except queue.Empty:
                        break
                waiting = ser.in_waiting   # raises once the USB device is gone
                if waiting:
                    text = decoder.decode(ser.read(waiting))
                    if text and generation == self._generation:
                        self.on_data(text)
                        self._watch_login(text)
                        self._watch_uboot(text, ser)
                elif self._tail_dirty:
                    self._answer_login(ser)
                if self._uboot is not None:
                    self._hold_uboot_key(ser)
                try:
                    data = outbox.get(timeout=_IDLE)
                except queue.Empty:
                    continue
                ser.write(data)
        except Exception as exc:
            if generation != self._generation:
                return   # closed on purpose
            self._closed = True
            self._generation += 1
            self.ser = None
            try:
                ser.close()
            except Exception:
                pass
            self.on_lost(f"串口 {self.port} 已断开（USB 串口线可能已拔出）：{exc}")

    def _watch_login(self, text):
        if self._auto_login:
            self._tail = (self._tail + strip_ansi(text))[-256:]
            self._tail_at = time.monotonic()
            self._tail_dirty = True

    def _answer_login(self, ser):
        """Answer a getty prompt with the saved credentials once it has gone quiet."""
        if time.monotonic() - self._tail_at < _PROMPT_SETTLE:
            return
        self._tail_dirty = False
        tail = self._tail
        if self._login_stage and _LOGIN_FAILED.search(tail):
            self._stop_auto_login("串口自动登录失败：用户名或密码错误，请在终端手动登录。")
        elif self._login_stage == "user" and _PASSWORD_PROMPT.search(tail):
            # Only right after our username: never feed su/passwd prompts.
            self._login_stage, self._tail = "password", ""
            ser.write((self._password + "\r").encode("utf-8"))
        elif _LOGIN_PROMPT.search(tail):
            if self._login_attempts >= _LOGIN_ATTEMPTS:
                self._stop_auto_login("串口自动登录未成功，请在终端手动登录。")
                return
            self._login_attempts += 1
            self._login_stage, self._tail = "user", ""
            ser.write((self._username + "\r").encode("utf-8"))
        elif self._login_stage and _SHELL_PROMPT.search(tail):
            self._login_stage, self._login_attempts = None, 0
            self.log("SYSTEM", "串口已自动登录。")

    def _watch_uboot(self, text, ser):
        state = self._uboot
        if state is None:
            return
        state["tail"] = (state["tail"] + strip_ansi(text))[-200:]
        if state["since"] is None:
            if _AUTOBOOT.search(state["tail"]):
                state["since"] = time.monotonic()
        elif _UBOOT_PROMPT.search(state["tail"]):
            self._uboot = None
            ser.write(b"\x03")   # drop the held keys echoed onto the U-Boot command line
            self.on_uboot(True)
        elif _KERNEL_STARTED.search(state["tail"]):
            self._uboot = None   # too late: stop typing into the booting kernel
            self.on_uboot(False)

    def _hold_uboot_key(self, ser):
        state = self._uboot
        now = time.monotonic()
        if state["since"] is None:
            if now > state["until"]:
                self._uboot = None
                self.on_uboot(False)
            return
        if now - state["since"] > _UBOOT_HOLD:
            self._uboot = None
            self.on_uboot(False)
        elif now >= state["next"]:
            state["next"] = now + _UBOOT_REPEAT
            ser.write(state["key"])

    def _stop_auto_login(self, message):
        self._auto_login = False
        self._login_stage = None
        self.log("ERROR", message)


def list_serial_ports():
    """[(device, description)] for attached COM ports, e.g. ('COM5', 'USB-SERIAL CH340').

    ``device`` is the connectable value; ``description`` is the human-readable
    board name shown next to it in the picker."""
    try:
        from serial.tools import list_ports
        return [(port.device, (port.description or "").strip()) for port in list_ports.comports()]
    except Exception:
        return []
