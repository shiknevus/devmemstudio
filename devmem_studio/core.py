# -*- coding: utf-8 -*-
"""Configuration, register semantics and SSH transport; independent of the GUI."""
from __future__ import annotations

import base64
import codecs
from contextlib import contextmanager
import copy
import hashlib
import hmac
import importlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import posixpath
import re
import select
import shlex
import socket
import sys
import tempfile
import threading
import time
import uuid

from .catalog import DEFAULT_CATEGORIES, DEFAULT_TYPES, NAME_GROUPS, REGISTER_FIELDS


class _LazyModule:
    """Imports on first attribute access: paramiko is about half of start-up time."""
    def __init__(self, name):
        self._name = name

    def __getattr__(self, attribute):
        return getattr(importlib.import_module(self._name), attribute)


paramiko = _LazyModule("paramiko")


def preload_ssh():
    """Warm the SSH stack off the GUI thread so the first connect does not wait for it."""
    try:
        importlib.import_module("paramiko")
    except Exception:
        pass   # the connect itself reports a broken SSH stack

DEFAULT_LOG_PATH = "/run/media/sda/sunny.log"
DEFAULT_BIT_DIR = "/run/media/sda"
BIT_DIR_HISTORY = 10


def normalize_remote_dir(text) -> str:
    """Absolute board directory: ' /run/media/sda/ ' -> '/run/media/sda'."""
    raw = str(text or "").strip().replace("\\", "/")
    if not raw.startswith("/") or any(ord(char) < 0x20 for char in raw):
        raise ValueError("板端目录须为以 / 开头的绝对路径，例如 /run/media/sda。")
    return "/" + posixpath.normpath(raw).lstrip("/")


def remember_dir(history, directory, limit=BIT_DIR_HISTORY) -> list[str]:
    """Most-recent-first directory list with `directory` moved to the front."""
    items = [item for item in history if isinstance(item, str) and item and item != directory]
    return [directory, *items][:limit]

# 寄存器按批读：每批一条 shell 命令一次往返，批大小兼顾 PTY 行缓冲与进度粒度
BATCH_READ_CHUNK = 32
# Register monitor: the devmem fallback forks one busybox devmem per register per sample
# (~1 ms each on the A53), so 5 ms is the shortest interval a few registers can hold there.
MONITOR_MIN_INTERVAL_MS = 5
MONITOR_MAX_REGISTERS = 8
# Resident sampler (tools/regmon): maps the register pages once and loops on board deadlines.
REGMON_RESOURCE = "assets/regmon/regmon-aarch64"
MONITOR_START_TIMEOUT_S = 5.0
MONITOR_STALE_TIMEOUT_S = 2.0   # silence beyond this (and 3 intervals) shows the run as paused
MONITOR_SETUP_TIMEOUT_S = 8.0


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        # A native single-file bootstrap may run the verified payload from its
        # per-user cache; portable preferences still belong beside the outer EXE.
        origin = os.environ.get("DEVMEMSTUDIO_LAUNCHER_DIR")
        if origin and Path(origin).is_absolute() and Path(origin).is_dir():
            return Path(origin)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def resource_path(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", application_dir())) / name


def user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "DevmemStudio"


def parse_addr(value):
    """Addresses without a prefix are still hexadecimal, as in the legacy app."""
    try:
        return int(str(value).strip(), 16)
    except (ValueError, TypeError):
        return None


def parse_int(value):
    try:
        text = str(value).strip()
        return int(text, 16 if text.lower().startswith(("0x", "-0x", "+0x")) else 10)
    except (ValueError, TypeError):
        return None


def validated_address(value) -> int:
    address = parse_addr(value)
    if address is None or not 0 <= address <= 0xFFFFFFFF:
        raise ValueError("地址须为 0x00000000–0xFFFFFFFF 的十六进制数。")
    return address


def access_width(register: dict) -> int:
    """Bit fields of 1/20 bits occupy a 32-bit bus word, not a devmem access size."""
    width = int(register.get("width", 32))
    return width if width in (8, 16, 32, 64) else 32


def write_value(text: str, fmt: str, width: int) -> int:
    try:
        value = int(str(text).strip(), 16 if fmt == "H" else 10)
    except (ValueError, TypeError):
        raise ValueError("写入值格式无效，请按所选 HEX / DEC 进制输入。") from None
    if width not in (8, 16, 32, 64):
        raise ValueError("访问位宽只能为 8、16、32 或 64 位。")
    if not 0 <= value < (1 << width):
        raise ValueError(f"写入值须在 0–{(1 << width) - 1} 之间（{width} 位无符号数）。")
    return value


def read_command(address: int) -> str:
    validated_address(hex(address))
    return f"devmem 0x{address:08x}"


def write_command(address: int, width: int, value: int) -> str:
    validated_address(hex(address))
    write_value(str(value), "D", width)
    if address % (width // 8):
        raise ValueError(f"0x{address:08X} 未按 {width} 位访问要求对齐。")
    return f"devmem 0x{address:08x} {width} 0x{value:x}"


def _strip_shell_prompt(line: str) -> str:
    """Remove a leading interactive prompt (root@host:~# , board# , $ , > …).

    The interactive shell prints PS1 before each line of a multi-line batch, and
    the batch's printf outputs no trailing newline, so prompt and value land on
    the same line: ``root@host:~# __Rb01102dc 0x001E3660``."""
    return re.sub(r"^(?:\S+@\S+:[^#\n]*|[^\s]*) ?[#$>]\s+", "", line, count=1)


def parse_devmem_read_output(text, addr_val=None):
    """Accept a value line or 'Value at address (...) : / = value'; reject echoes/errors."""
    if not text:
        return None, None
    candidates = []
    number = r"(0[xX][0-9a-fA-F]+|[0-9]+)"
    for line in strip_ansi(text).splitlines():
        line = _strip_shell_prompt(line.strip())
        match = re.fullmatch(number, line)
        if not match and re.search(r"\b(value|read|data)\b", line, re.I):
            match = re.search(r"[:=]\s*" + number + r"\s*$", line)
        if not match and re.match(r"^0[xX][0-9a-fA-F]+\s*[:=]", line):
            prefix = re.match(r"^(0[xX][0-9a-fA-F]+)", line).group(1)
            if addr_val is None or int(prefix, 16) == addr_val:
                match = re.search(r"[:=]\s*" + number + r"\s*$", line)
        if match:
            raw = match.group(1)
            candidates.append((int(raw, 16 if raw.lower().startswith("0x") else 10), raw))
    return candidates[-1] if candidates else (None, None)


def decode_fields(name: str, value: int | None) -> dict[str, int]:
    if value is None:
        return {}
    value &= 0xFFFFFFFF
    return {field: (value >> low) & ((1 << (high - low + 1)) - 1)
            for field, high, low, _ in REGISTER_FIELDS.get(name, ())}


def format_decoded_fields(name: str, value: int | None) -> dict[str, str]:
    fields = decode_fields(name, value)
    return {field: f"0x{fields[field]:02X}" if radix == "HEX" else str(fields[field])
            for field, _, _, radix in REGISTER_FIELDS.get(name, ()) if field in fields}


def register_group(register: dict) -> str:
    return NAME_GROUPS.get(register["name"], (98, "其他"))[1]


def monitor_script(addresses, interval_ms) -> str:
    """Board-side sampler: an '@<ns>' stamp line, then one devmem value (or x) per address.

    Sleeps to deadlines on the board clock, so the mean period holds the interval while
    the reads fit inside it; without `date +%N` it falls back to a fixed sleep."""
    if not addresses:
        raise ValueError("请至少选择一个寄存器。")
    if len(addresses) > MONITOR_MAX_REGISTERS:
        raise ValueError(f"最多同时监视 {MONITOR_MAX_REGISTERS} 个寄存器。")
    if not math.isfinite(float(interval_ms)) or not MONITOR_MIN_INTERVAL_MS <= float(interval_ms) <= 60000:
        raise ValueError(f"采样间隔须为 {MONITOR_MIN_INTERVAL_MS}–60000 ms。")
    if any(validated_address(hex(address)) % 4 for address in addresses):
        raise ValueError("监视地址须按 32 位访问要求对齐。")
    period = max(1, round(float(interval_ms) * 1000))   # µs
    reads = "; ".join(f"devmem 0x{validated_address(hex(a)):08x} || echo x" for a in addresses)
    # SSH exec shells do not source the login profile; embedded boards often keep
    # devmem in /sbin, outside their non-interactive PATH. Preserve custom tools
    # already on PATH and add the board's standard executable directories.
    return (f"P={period}; "
            'PATH="$PATH:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:/run/media/sda/bin"; export PATH; '
            "command -v devmem >/dev/null 2>&1 || { echo 'devmem: not found' >&2; exit 127; }; "
            "n=0; c=$(date +%s%N); case $c in ''|*[!0-9]*) c=;; esac; "
            "if command -v usleep >/dev/null 2>&1; then Z=1; else Z=; fi; "
            "while :; do "
            'if [ -n "$c" ]; then t=$(date +%s%N); else t=; fi; echo "@$t" || exit; '
            f"{reads}; "
            'if [ -n "$t" ]; then t=$((t / 1000)); n=$((n ? n + P : t + P)); n=$((n < t ? t : n)); d=$((n - t)); else d=$P; fi; '
            'if [ $d -gt 0 ]; then if [ -n "$Z" ]; then usleep $d; '
            "else sleep $((d / 1000000)).$(printf %06d $((d % 1000000))); fi; fi; "
            "done")


def monitor_command(addresses, interval_ms, sampler=None) -> str:
    """A resident sampler the board cannot execute (shell status 126/127, e.g. /tmp mounted
    noexec) hands over to the devmem loop. Any other failure ends the run, so a faulting
    address is never retried in devmem."""
    script = monitor_script(addresses, interval_ms)
    if not sampler:
        return "echo '#shell'; " + script
    period = max(1, round(float(interval_ms) * 1000))
    return (f"{shlex.quote(sampler)} {period} " + " ".join(f"0x{a:08x}" for a in addresses)
            + "; s=$?; [ $s -eq 126 ] || [ $s -eq 127 ] || exit $s; echo '#shell'; " + script)


class MonitorParser:
    """Assemble the sampler stream into (seconds, values) samples; None marks a failed read."""

    def __init__(self, count, clock=time.time):
        self.count = count
        self.clock = clock
        self.samples = []
        self.received = 0
        self.note = ""      # last stray line, e.g. a devmem or shell error
        self.sampler = ""   # from the '#regmon' / '#shell' banner
        self._buffer = b""
        self._stamp = None
        self._values = []
        self._last_stamp = None

    def feed(self, data):
        *lines, self._buffer = (self._buffer + data).split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("@"):
                digits = line[1:]
                self._stamp = int(digits) / 1e9 if digits.isdigit() else self.clock()
                self._values = []
            elif line.startswith("#"):
                kind = (line[1:].split() or [""])[0]
                if self.sampler and kind != self.sampler:
                    raise ValueError("采样方式在运行中改变，监视已停止，避免混用时间基准。")
                self.sampler = kind
            elif self._stamp is not None and (line == "x" or re.fullmatch(r"0[xX][0-9a-fA-F]+", line)):
                self._values.append(None if line == "x" else int(line, 16))
                if len(self._values) == self.count:
                    if self._last_stamp is not None and self._stamp < self._last_stamp:
                        raise ValueError("板端时间倒退，监视已停止，避免绘制错误时间轴。")
                    self._last_stamp = self._stamp
                    self.samples.append((self._stamp, tuple(self._values)))
                    self.received += 1
                    self._stamp = None
            elif line:
                self.note = line

    def take(self):
        samples, self.samples = self.samples, []
        return samples


def normalize_monitor_settings(value):
    value = value if isinstance(value, dict) else {}
    selections = {}
    saved = value.get("selections")
    for key, addresses in (saved.items() if isinstance(saved, dict) else ()):
        if isinstance(key, str) and isinstance(addresses, list):
            selections[key] = list(dict.fromkeys(address for address in addresses
                                                if type(address) is int and 0 <= address <= 0xFFFFFFFF))[:MONITOR_MAX_REGISTERS]
    return {"signed": value.get("signed") is True, "selections": selections}


def default_config() -> dict:
    return {"host": "", "port": 22, "username": "root", "password": "",
            "base": "0xb0100000", "default_width": 32, "last_category": "axis",
            "last_address": "0x0800", "remember_password": False,
            "top_path": "", "poll_interval": 1000, "log_path": DEFAULT_LOG_PATH, "write_cache": {},
            "serial_port": "", "serial_baud": 115200, "serial_username": "",
            "serial_password": "", "remember_serial_password": False, "inspector_mode": "bits",
            "bitpack_settings": {}, "bit_remote_dir": DEFAULT_BIT_DIR, "bit_remote_dirs": [DEFAULT_BIT_DIR],
            "bit_local_dir": "", "monitor_interval_ms": 10, "monitor_settings": normalize_monitor_settings(None)}


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.warning = ""
        self.path = Path(path) if path else self._choose_path()

    def _choose_path(self) -> Path:
        portable = application_dir() / "registers.json"
        fallback = user_data_dir() / "registers.json"
        # Prefer portable mode when writable; installations in Program Files use local app data.
        try:
            with tempfile.TemporaryFile(dir=portable.parent):
                pass
            return portable
        except OSError:
            if not fallback.exists() and portable.exists():
                try:
                    fallback.parent.mkdir(parents=True, exist_ok=True)
                    fallback.write_bytes(portable.read_bytes())
                except OSError:
                    self.warning = "配置目录不可写，当前会话的设置可能无法保存。"
            return fallback

    def load(self) -> dict:
        cfg = default_config()
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise ValueError("配置根节点须为对象")
                cfg.update({k: v for k, v in data.items() if k not in ("types", "categories")})
                if "remember_password" not in data:
                    cfg["remember_password"] = bool(data.get("password"))
            except (OSError, ValueError) as exc:
                self.warning = f"配置读取失败，已使用默认值：{exc}"
                # Keep a recovery copy before any later save.
                try:
                    self.path.with_name(self.path.name + ".invalid-" + time.strftime("%Y%m%d-%H%M%S")).write_bytes(self.path.read_bytes())
                except OSError:
                    pass
        if cfg.get("last_category") not in DEFAULT_CATEGORIES:
            cfg["last_category"] = "axis"
        for field, fallback, low, high in (("port", 22, 1, 65535),
                                            ("poll_interval", 1000, 100, 120000),
                                            ("monitor_interval_ms", 10, MONITOR_MIN_INTERVAL_MS, 60000)):
            val = parse_int(cfg.get(field))
            cfg[field] = val if val is not None and low <= val <= high else fallback
        baud = parse_int(cfg.get("serial_baud"))
        cfg["serial_baud"] = baud if baud is not None and 1200 <= baud <= 10000000 else 115200
        for field in ("base", "last_address"):
            try:
                # Numeric JSON values are ambiguous for hexadecimal addresses and
                # cannot be passed to QLineEdit.setText. Do not guess an address.
                if not isinstance(cfg[field], str):
                    raise ValueError("地址配置须为字符串")
                validated_address(cfg[field])
            except (ValueError, TypeError):
                cfg[field] = default_config()[field]
        for field in ("host", "username", "password", "log_path", "top_path",
                      "serial_port", "serial_username", "serial_password"):
            cfg[field] = str(cfg.get(field) or "")
        for flag, secret in (("remember_password", "password"),
                             ("remember_serial_password", "serial_password")):
            # Only an explicit JSON true opts into retaining credentials.
            cfg[flag] = cfg.get(flag) is True
            if not cfg[flag]:
                cfg[secret] = ""
        if not isinstance(cfg.get("write_cache"), dict):
            cfg["write_cache"] = {}
        if cfg.get("inspector_mode") not in ("bits", "write"):
            cfg["inspector_mode"] = "bits"
        try:
            cfg["bit_remote_dir"] = normalize_remote_dir(cfg.get("bit_remote_dir"))
        except ValueError:
            cfg["bit_remote_dir"] = DEFAULT_BIT_DIR
        history = []
        for item in cfg.get("bit_remote_dirs") if isinstance(cfg.get("bit_remote_dirs"), list) else []:
            try:
                history.append(normalize_remote_dir(item))
            except ValueError:
                pass
        cfg["bit_remote_dirs"] = remember_dir(history or [DEFAULT_BIT_DIR], cfg["bit_remote_dir"])
        cfg["bit_local_dir"] = cfg["bit_local_dir"] if isinstance(cfg.get("bit_local_dir"), str) else ""
        cfg["monitor_settings"] = normalize_monitor_settings(cfg.get("monitor_settings"))
        return cfg

    def save(self, cfg: dict):
        data = copy.deepcopy(cfg)
        data.pop("types", None)
        data.pop("categories", None)
        for flag, secret in (("remember_password", "password"),
                             ("remember_serial_password", "serial_password")):
            data[flag] = data.get(flag) is True
            if not data[flag]:
                data[secret] = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Replace atomically so loss of power doesn't leave half a JSON document.
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".devmem-", suffix=".tmp", delete=False) as handle:
                temp_name = handle.name
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", text).replace("\r", "")


class CommandError(RuntimeError):
    pass


class ReadbackError(CommandError):
    """The device accepted a write but its subsequent value could not be read."""


def host_key_fingerprint(key):
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class HostKeyChangedError(Exception):
    """A specific observed host key change, retained for explicit confirmation."""
    def __init__(self, hostname, port, key, expected_key):
        super().__init__(hostname, key, expected_key)
        self.hostname, self.key, self.expected_key = hostname, key, expected_key
        self.port = port
        self.hostkey_name = hostname if port == 22 else f"[{hostname}]:{port}"
        self.old_fingerprint = host_key_fingerprint(expected_key)
        self.new_fingerprint = host_key_fingerprint(key)

    def __str__(self):
        return f"{self.hostname}:{self.port} 的 SSH 主机密钥已变化，请核对新指纹后更新连接记录。"


def _matches_host(token, hostname):
    if token == hostname:
        return True
    if token.startswith("|1|"):
        try:
            return hmac.compare_digest(paramiko.HostKeys.hash_host(hostname, token), token)
        except (ValueError, AssertionError, IndexError):
            return False
    return False


_board_transport = None


def board_transport():
    """paramiko.Transport subclass, built on first use since paramiko loads lazily."""
    global _board_transport
    if _board_transport is None:
        class BoardTransport(paramiko.Transport):
            """Keep RSA/SHA-2 ahead of legacy RSA even for a saved RSA host key."""
            def start_client(self, event=None, timeout=None):
                options = self.get_security_options()
                algorithms = list(options.key_types)
                if "ssh-rsa" in algorithms:
                    # SSHClient promotes the saved key's type (ssh-rsa) before calling
                    # start_client. Prefer stronger signatures for that same trusted key.
                    modern_rsa = [name for name in ("rsa-sha2-512", "rsa-sha2-256") if name in algorithms]
                    algorithms = [name for name in algorithms if name not in modern_rsa]
                    position = algorithms.index("ssh-rsa")
                    algorithms[position:position] = modern_rsa
                    options.key_types = tuple(algorithms)
                return super().start_client(event=event, timeout=timeout)
        _board_transport = BoardTransport
    return _board_transport


def _sftp_exists(sftp, path) -> bool:
    try:
        sftp.stat(path)
        return True
    except IOError:
        return False


def _sftp_remove_quietly(sftp, path):
    try:
        sftp.remove(path)
    except Exception:
        pass


@contextmanager
def _atomic_download_target(local_path):
    """Replace the chosen file only after a complete download; remove partials."""
    target = Path(local_path)
    temporary = None
    try:
        # Same directory keeps os.replace atomic, including on Windows. Close
        # this handle before SFTP opens the file (Windows does not share it).
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.",
                                         suffix=".part", delete=False) as handle:
            temporary = Path(handle.name)
        yield temporary
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class SshSession:
    """Persistent command shell and a separate stream channel, with serialized commands."""
    def __init__(self, log=lambda level, text: None, data_dir: Path | None = None):
        self.log = log
        self.client = None
        self.chan = None
        self.last_exit = None
        self.data_dir = Path(data_dir) if data_dir else user_data_dir()
        self._lock = threading.Lock()
        self._stream_channel = None
        self._stream_stop = threading.Event()
        self._stream_thread = None
        self._generation = 0
        self._connect_sock = None   # live during connect(); close() aborts a handshake in flight
        self._regmon = (None, None)   # (transport, architecture); placement failures are never cached

    @property
    def alive(self):
        try:
            return bool(self.client and self.client.get_transport() and self.client.get_transport().is_active()
                        and self.chan and not self.chan.closed)
        except Exception:
            return False

    def _connect_socket_abortably(self, host, port, timeout, generation):
        """TCP connect in cancel-checkable slices.

        A blocked ``socket.create_connection`` to an unreachable board cannot
        be interrupted (there is no socket to shutdown yet), so the handshake
        waits out the whole timeout. Here the SYN wait is polled with select()
        and the session generation is re-checked each slice: a cancel wins
        immediately."""
        last_error = None
        for family, kind, proto, _null, sockaddr in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
            sock = socket.socket(family, kind, proto)
            sock.setblocking(False)
            try:
                try:
                    sock.connect(sockaddr)
                except BlockingIOError:
                    deadline = time.monotonic() + timeout
                    while True:
                        if generation != self._generation:
                            raise CommandError("连接已取消。")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise socket.timeout("timed out")
                        _, writable, _ = select.select([], [sock], [], min(0.1, remaining))
                        if not writable:
                            continue
                        error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        if error:
                            raise OSError(error, os.strerror(error))
                        break
                sock.setblocking(True)
                sock.settimeout(timeout)
                return sock
            except CommandError:
                sock.close()
                raise
            except OSError as exc:
                sock.close()
                last_error = exc
        raise last_error or OSError("无法建立 TCP 连接")

    def connect(self, host, port, username, password, timeout=8):
        self.close()
        generation = self._generation
        client = paramiko.SSHClient()
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            known_hosts = self.data_dir / "known_hosts"
            if not known_hosts.exists():
                known_hosts.touch()
            client.load_host_keys(str(known_hosts))
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                # Abortable TCP connect: a cancel during the SYN wait breaks out
                # immediately instead of waiting out the timeout.
                sock = self._connect_socket_abortably(host, port, timeout, generation)
                self._connect_sock = sock
                if generation != self._generation:
                    # Cancelled while the socket was being created (close()
                    # found nothing to abort): stop before the handshake waits.
                    raise CommandError("连接已取消。")
                client.connect(hostname=host, port=port, username=username, password=password,
                               timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                               channel_timeout=timeout, allow_agent=False, look_for_keys=False,
                               sock=sock, transport_factory=board_transport())
            except paramiko.BadHostKeyException as exc:
                raise HostKeyChangedError(host, port, exc.key, exc.expected_key) from exc
            except paramiko.ssh_exception.IncompatiblePeer as exc:
                kind = "主机密钥" if "host key" in str(exc) else "密钥交换" if "kex" in str(exc) else "加密"
                raise CommandError(f"SSH {kind}算法不兼容，请检查设备 SSH 配置。原始信息：{exc}") from exc
            transport = client.get_transport()
            transport.set_keepalive(15)
            compatibility = "（旧设备兼容）" if transport.host_key_type == "ssh-rsa" else ""
            self.log("SYSTEM", f"SSH 协商完成：主机密钥 {transport.host_key_type}{compatibility}；加密 {transport.local_cipher}。")
            channel = client.invoke_shell(width=240, height=40)
            channel.settimeout(timeout)
            channel.sendall(b"stty -echo\n")
            # Suppress welcome banners and command echoes before the first operation.
            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline:
                if channel.recv_ready():
                    channel.recv(65536)
                time.sleep(0.02)
            if generation != self._generation:
                raise CommandError("连接已取消。")
            self.client, self.chan = client, channel
            self._connect_sock = None
        except Exception:
            sock, self._connect_sock = self._connect_sock, None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            client.close()
            raise

    def replace_host_key(self, change: HostKeyChangedError):
        """Persist only the key shown in the confirmation, preserving other hosts."""
        path = self.data_dir / "known_hosts"
        original = path.read_bytes()
        keys = paramiko.HostKeys(str(path))
        current = keys.lookup(change.hostkey_name)
        if (path.read_bytes() != original or not current
                or not any(key == change.expected_key for key in current.values())):
            raise CommandError("已保存的主机密钥记录已变化，请重新连接并核对指纹。")
        retained = []
        for line in original.decode("utf-8").splitlines(keepends=True):
            match = re.match(r"^([ \t]*)(\S+)([ \t]+)(.*?)(\r?\n)?$", line)
            if match is None or match[2].startswith("#"):
                retained.append(line)
                continue
            hosts = match[2].split(",")
            others = [name for name in hosts if not _matches_host(name, change.hostkey_name)]
            if len(others) == len(hosts):
                retained.append(line)
            elif others:
                retained.append(match[1] + ",".join(others) + match[3] + match[4] + (match[5] or ""))
        content = "".join(retained)
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"{change.hostkey_name} {change.key.get_name()} {change.key.get_base64()}\n"
        backup = path.with_name(f"known_hosts.backup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix="known_hosts.", suffix=".tmp", delete=False) as handle:
                temp_name = handle.name
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            # Do not overwrite a record another process changed during review.
            if path.read_bytes() != original:
                raise CommandError("已保存的主机密钥记录已变化，请重新连接并核对指纹。")
            with backup.open("xb") as handle:
                handle.write(original)
            os.replace(temp_name, path)
            self.log("SYSTEM", f"已更新 {change.hostname}:{change.port} 的主机密钥：{change.new_fingerprint}；原记录已备份。")
            return backup
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def run(self, command: str, timeout=8, quiet=False):
        if not command.strip() or "\x00" in command:
            raise ValueError("命令不能为空或包含空字符。")
        with self._lock:
            if not self.alive:
                raise CommandError("SSH 已断开，请重新连接设备。")
            channel = self.chan
            self.last_exit = None
            while channel.recv_ready():
                channel.recv(65536)
            if not quiet:
                self.log("CMD", command)
            marker = "__DM_" + uuid.uuid4().hex + "_"
            end_re = re.compile(r"(?:^|\n)" + re.escape(marker) + r"(\d+)__\s*(?:\n|$)")
            full = command.rstrip() + "\nprintf '\\n" + marker + "%s__\\n' \"$?\"\n"
            channel.sendall(full.encode("utf-8"))
            output = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if channel.recv_ready():
                    chunk = channel.recv(65536)
                    if not chunk:
                        raise CommandError("SSH 连接已断开。")
                    output.extend(chunk)
                    if len(output) > 4 * 1024 * 1024:
                        self.close()
                        raise CommandError("命令输出超过 4 MB，已断开连接；连续日志请使用板端日志。")
                    decoded = strip_ansi(output.decode("utf-8", "replace"))
                    match = end_re.search(decoded)
                    if match:
                        self.last_exit = int(match.group(1))
                        lines = []
                        for line in decoded[:match.start()].splitlines():
                            line = _strip_shell_prompt(line)
                            if (not line.strip() or marker in line
                                    or line.strip() == command.strip()
                                    or re.fullmatch(r"[^\n]*[#$>]\s*", line)):
                                continue
                            lines.append(line)
                        result = "\n".join(lines).strip()
                        if result and not quiet:
                            self.log("INFO" if self.last_exit == 0 else "ERROR", result)
                        if self.last_exit:
                            raise CommandError(f"命令退出码 {self.last_exit}：{result or command}")
                        return result
                if channel.closed or not self.client or not self.client.get_transport().is_active():
                    raise CommandError("SSH 连接已断开，设备可能正在重启。")
                time.sleep(0.015)
            # A shell without a terminator is not safe to reuse for the next register read.
            self.close()
            raise TimeoutError(f"命令超过 {timeout:g} 秒未完成，连接已关闭，请重新连接。")

    def read(self, address: int, quiet=False) -> int:
        output = self.run(read_command(address), quiet=quiet)
        value, _ = parse_devmem_read_output(output, address)
        if value is None:
            raise CommandError("未识别到寄存器数值：" + (output[:240] or "设备未返回数据"))
        return value

    def read_many(self, addresses):
        """Batch read in one shell round trip; returns {address: (value, error)}.

        Each address is echoed as a __Rxxxxxxxx tag right before its devmem value,
        so a failing or silent devmem only marks its own register, never the batch.
        The session log condenses a multi-register batch to one CMD + one result
        line instead of echoing every printf/devmem pair."""
        addresses = [validated_address(hex(address)) for address in addresses]
        if not addresses:
            return {}
        if len(addresses) == 1:
            try:
                value = self.read(addresses[0])
                return {addresses[0]: (value, None)}
            except Exception as exc:
                return {addresses[0]: (None, str(exc))}
        lines = [f"printf '__R{a:08x} '; devmem 0x{a:08x} || true" for a in addresses]
        address_list = " ".join(f"0x{a:08x}" for a in addresses)
        self.log("CMD", f"读取 {len(addresses)} 个寄存器 {address_list}")
        output = self.run("\n".join(lines), quiet=True)
        found = {}
        # Split on tags, not lines: a silent devmem puts the next tag on the same line.
        parts = re.split(r"__R([0-9a-fA-F]{8})", output)
        for tag, text in zip(parts[1::2], parts[2::2]):
            found[int(tag, 16)] = next((line.strip() for line in text.splitlines() if line.strip()), "")
        results = {}
        for address in addresses:
            text = found.get(address)
            value, _ = parse_devmem_read_output(text, address) if text else (None, None)
            if value is None:
                results[address] = (None, "未识别到寄存器数值：" + (text[:240] if text else "设备未返回数据"))
            else:
                results[address] = (value, None)
        failed = [f"0x{address:08X}" for address, (value, _) in results.items() if value is None]
        if failed:
            self.log("ERROR", f"读取 {len(results) - len(failed)}/{len(results)} 成功，失败：{'、'.join(failed)}")
        else:
            values = "  ".join(f"0x{address:08x}=0x{results[address][0]:08X}" for address in addresses)
            self.log("INFO", values)
        return results

    def write(self, address: int, width: int, value: int, quiet=False) -> int:
        self.run(write_command(address, width, value), quiet=quiet)
        # Readback is observed data; self-clearing/action registers need not equal the write value.
        try:
            return self.read(address, quiet=quiet)
        except Exception as exc:
            raise ReadbackError(f"0x{address:08X} 写入已完成，但回读失败：{exc}") from exc

    def upload_bitfile(self, local_path, remote_dir="/run/media/sda",
                       dest_name="sunny_fpga.bit", timestamp=None, progress=None):
        """Upload the new bit beside the live one, then swap it in with a timestamped backup.

        The remote directory and any parent path components are created on demand. The
        file is staged as <dest>.uploading, so a failed or cut-off transfer never touches
        the live sunny_fpga.bit; only then is the old bit renamed to sunny_fpga.bit_<timestamp>.
        Runs on the worker thread; `progress(done, total)` reports byte transfer."""
        if not self.alive:
            raise CommandError("请先连接设备。")
        stamp = timestamp or time.strftime("%Y%m%d%H%M%S")
        transport = self.client.get_transport()
        sftp = paramiko.SFTPClient.from_transport(transport) if transport else None
        try:
            if sftp is None:
                raise CommandError("SSH 通道不可用，无法上传。")
            if remote_dir and remote_dir != ".":
                parts = [p for p in remote_dir.replace("\\", "/").split("/") if p]
                current = "/"
                for part in parts:
                    current = current.rstrip("/") + "/" + part
                    try:
                        sftp.stat(current)
                    except IOError:
                        sftp.mkdir(current)
            folder = remote_dir.rstrip("/")
            remote_path = f"{folder}/{dest_name}"
            staging_path = f"{remote_path}.uploading"
            total = os.path.getsize(local_path)
            sent = [0]

            def callback(transferred, _total):
                if progress is not None and transferred > sent[0]:
                    sent[0] = transferred
                    progress(transferred, total)

            self.log("CMD", f"put {local_path} -> {staging_path}")
            try:
                sftp.put(local_path, staging_path, callback=callback)
            except Exception:
                _sftp_remove_quietly(sftp, staging_path)
                raise
            backup_name = f"{dest_name}_{stamp}"
            backup_path = f"{folder}/{backup_name}"
            if _sftp_exists(sftp, remote_path):
                self.log("CMD", f"rename {remote_path} -> {backup_name}")
                try:
                    sftp.rename(remote_path, backup_path)
                except Exception:
                    _sftp_remove_quietly(sftp, staging_path)
                    raise
            else:
                backup_path = None  # no previous bit to back up
            try:
                sftp.rename(staging_path, remote_path)
            except Exception:
                if backup_path:
                    try:
                        sftp.rename(backup_path, remote_path)  # put the old bit back
                    except Exception:
                        pass
                raise
            try:
                attrs = sftp.stat(remote_path)
                self.log("INFO", f"{remote_path}: {attrs.st_size} 字节")
            except IOError:
                pass
            return backup_name if backup_path else None
        finally:
            if sftp is not None:
                sftp.close()

    def list_bit_backups(self, remote_dir="/run/media/sda", bitname="sunny_fpga.bit"):
        """List bit files on the board: the current bit plus timestamped backups.

        Returns [{name, timestamp}] with the current bit first (timestamp None),
        then backups newest first. timestamp is the suffix after '{bitname}_'."""
        if not self.alive:
            raise CommandError("请先连接设备。")
        transport = self.client.get_transport()
        sftp = paramiko.SFTPClient.from_transport(transport) if transport else None
        try:
            if sftp is None:
                raise CommandError("SSH 通道不可用，无法读取板端目录。")
            prefix = bitname + "_"
            try:
                names = [name for name in sftp.listdir(remote_dir)
                         if name == bitname or name.startswith(prefix)]
            except IOError:
                raise CommandError(f"板端目录不存在：{remote_dir}")
            entries = [{"name": bitname, "timestamp": None}] if bitname in names else []
            backups = sorted(({"name": n, "timestamp": n[len(prefix):]} for n in names
                              if n.startswith(prefix)), key=lambda item: item["timestamp"], reverse=True)
            return entries + backups
        finally:
            if sftp is not None:
                sftp.close()

    def list_dirs(self, parent="/run/media"):
        """Sub-directories of `parent` on the board, e.g. the mounts under /run/media."""
        if not self.alive:
            raise CommandError("请先连接设备。")
        transport = self.client.get_transport()
        sftp = paramiko.SFTPClient.from_transport(transport) if transport else None
        try:
            if sftp is None:
                raise CommandError("SSH 通道不可用，无法读取板端目录。")
            try:
                entries = sftp.listdir_attr(parent)
            except IOError:
                return []
            return sorted(f"{parent.rstrip('/')}/{entry.filename}" for entry in entries
                          if (entry.st_mode or 0) & 0o170000 == 0o040000)
        finally:
            if sftp is not None:
                sftp.close()

    def rollback_bit(self, backup_name, remote_dir="/run/media/sda",
                     dest_name="sunny_fpga.bit", timestamp=None, progress=None):
        """Move the current bit aside with a fresh timestamp and restore a chosen backup.

        The live sunny_fpga.bit (if present) is renamed to sunny_fpga.bit_<timestamp>,
        then the chosen backup is renamed back to sunny_fpga.bit. Runs on the worker
        thread; `progress(done, total)` reports the step."""
        if not self.alive:
            raise CommandError("请先连接设备。")
        stamp = timestamp or time.strftime("%Y%m%d%H%M%S")
        transport = self.client.get_transport()
        sftp = paramiko.SFTPClient.from_transport(transport) if transport else None
        try:
            if sftp is None:
                raise CommandError("SSH 通道不可用，无法回退。")
            dest = f"{remote_dir.rstrip('/')}/{dest_name}"
            backup = f"{remote_dir.rstrip('/')}/{backup_name}"
            if backup_name == dest_name or not backup_name.startswith(dest_name + "_"):
                raise CommandError("请选择带时间戳的 bit 备份。")
            try:
                sftp.stat(backup)
            except IOError:
                raise CommandError(f"备份文件不存在：{backup_name}")
            aside = f"{remote_dir.rstrip('/')}/{dest_name}_{stamp}"
            if _sftp_exists(sftp, dest):
                self.log("CMD", f"rename {dest_name} -> {dest_name}_{stamp}")
                sftp.rename(dest, aside)
            else:
                aside = None  # no current bit to move aside
            if progress:
                progress(50, 100)
            self.log("CMD", f"rename {backup_name} -> {dest_name}")
            try:
                sftp.rename(backup, dest)
            except Exception:
                if aside:
                    try:
                        sftp.rename(aside, dest)  # never leave the board without a bit
                    except Exception:
                        pass
                raise
            if progress:
                progress(100, 100)
            self.log("INFO", f"回退完成：{backup_name} -> {dest_name}")
        finally:
            if sftp is not None:
                sftp.close()

    def download_file(self, remote_path, local_path, progress=None):
        """Download a remote file via SFTP; progress(done, total) reports byte transfer."""
        if not self.alive:
            raise CommandError("请先连接设备。")
        transport = self.client.get_transport()
        sftp = paramiko.SFTPClient.from_transport(transport) if transport else None
        try:
            if sftp is None:
                raise CommandError("SSH 通道不可用，无法下载。")
            attrs = sftp.stat(remote_path)
            total = attrs.st_size
            sent = [0]

            def callback(transferred, _total):
                if progress is not None and transferred > sent[0]:
                    sent[0] = transferred
                    progress(transferred, total)

            self.log("CMD", f"get {remote_path} -> {local_path}")
            with _atomic_download_target(local_path) as temporary:
                sftp.get(remote_path, str(temporary), callback=callback)
            self.log("INFO", f"{local_path}: {os.path.getsize(local_path)} 字节")
        finally:
            if sftp is not None:
                sftp.close()

    def start_stream(self, path: str, output, stopped, cancel_event=None):
        stop = cancel_event if cancel_event is not None else threading.Event()
        if stop.is_set():
            return False
        if not self.alive:
            raise CommandError("请先连接设备。")
        if not path.strip() or "\x00" in path or "\n" in path:
            raise ValueError("请输入有效的板端日志路径。")
        self.stop_stream()
        self._stream_stop = stop
        command = "tail -f " + shlex.quote(path)
        if stop.is_set():
            return False
        channel = self.client.get_transport().open_session(timeout=8)
        self._stream_channel = channel
        try:
            if stop.is_set():
                channel.close()
                return False
            channel.exec_command(command)
            if stop.is_set():
                channel.close()
                return False
        except Exception:
            channel.close()
            if stop.is_set():
                return False
            raise
        self.log("CMD", command)

        def reader():
            decoders = [codecs.getincrementaldecoder("utf-8")("replace") for _ in range(2)]
            try:
                while not stop.is_set():
                    for index, (ready, receive) in enumerate(((channel.recv_ready, channel.recv),
                                                               (channel.recv_stderr_ready, channel.recv_stderr))):
                        if ready():
                            text = decoders[index].decode(receive(32768))
                            if text:
                                output(strip_ansi(text))
                    if (channel.closed or channel.exit_status_ready()) and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break
                    stop.wait(0.08)
                if not stop.is_set() and channel.exit_status_ready():
                    status = channel.recv_exit_status()
                    if status > 0:
                        output(f"\n日志命令退出，状态码 {status}。\n")
            except Exception as exc:
                if not stop.is_set():
                    output(f"\n日志连接中断：{exc}\n")
            finally:
                channel.close()
                stopped()
        self._stream_thread = threading.Thread(target=reader, daemon=True, name="board-log")
        self._stream_thread.start()
        return True

    def stop_stream(self):
        self._stream_stop.set()
        if self._stream_channel:
            self._stream_channel.close()
        self._stream_channel = None

    @contextmanager
    def _monitor_channel(self, cancel_event=None, timeout=MONITOR_SETUP_TIMEOUT_S, keep_open=False):
        """Bound channel setup and SFTP handshakes, including calls that ignore settimeout."""
        stop = cancel_event if cancel_event is not None else threading.Event()
        if stop.is_set():
            raise CommandError("监视启动已取消。")
        deadline = time.monotonic() + timeout
        channel = self.client.get_transport().open_session(timeout=min(timeout, 2.0))
        channel.settimeout(timeout)
        finished, expired = threading.Event(), threading.Event()

        def guard():
            while not finished.wait(0.05):
                if stop.is_set() or time.monotonic() >= deadline:
                    if not stop.is_set():
                        expired.set()
                    channel.close()
                    return

        watcher = threading.Thread(target=guard, daemon=True, name="monitor-setup-guard")
        watcher.start()
        completed = False
        try:
            yield channel
            if stop.is_set():
                raise CommandError("监视启动已取消。")
            if expired.is_set():
                raise TimeoutError("板端采样程序准备超时。")
            completed = True
        except Exception as exc:
            if stop.is_set():
                raise CommandError("监视启动已取消。") from exc
            if expired.is_set():
                raise TimeoutError("板端采样程序准备超时。") from exc
            raise
        finally:
            finished.set()
            if not keep_open or not completed:
                channel.close()
            watcher.join(0.1)

    def _exec_output(self, command, timeout=8, cancel_event=None):
        """Run a short command on its own exec channel; returns (exit status, output)."""
        with self._monitor_channel(cancel_event, timeout) as channel:
            channel.set_combine_stderr(True)
            channel.settimeout(timeout)
            channel.exec_command(command)
            chunks = []
            while chunk := channel.recv(65536):
                chunks.append(chunk)
            return channel.recv_exit_status(), b"".join(chunks).decode("utf-8", "replace")

    @contextmanager
    def _monitor_sftp(self, cancel_event):
        with self._monitor_channel(cancel_event) as channel:
            channel.invoke_subsystem("sftp")
            sftp = paramiko.SFTPClient(channel)
            try:
                yield sftp
            finally:
                sftp.close()

    def monitor_sampler(self, cancel_event=None):
        """Verify cached executable contents; retry transient placement failures on next start."""
        stop = cancel_event if cancel_event is not None else threading.Event()
        transport = self.client.get_transport()
        path = None
        try:
            if stop.is_set():
                raise CommandError("监视启动已取消。")
            data = resource_path(REGMON_RESOURCE).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            remote = f"/tmp/devmem-studio-regmon-{digest[:12]}"
            if self._regmon[0] is not transport:
                _, output = self._exec_output("uname -m", cancel_event=stop)
                self._regmon = (transport, output.strip())   # cache architecture only
            arch = self._regmon[1]
            if arch in ("aarch64", "arm64"):
                with self._monitor_sftp(stop) as sftp:
                    staging = None

                    def verified(candidate):
                        try:
                            with sftp.open(candidate, "rb") as handle:
                                content = handle.read(len(data) + 1)
                            if stop.is_set():
                                raise CommandError("监视启动已取消。")
                            return hashlib.sha256(content).hexdigest() == digest
                        except FileNotFoundError:
                            return False
                        except OSError as exc:
                            if getattr(exc, "errno", None) == 2:
                                return False
                            raise

                    try:
                        if stop.is_set():
                            raise CommandError("监视启动已取消。")
                        if verified(remote):
                            sftp.chmod(remote, 0o755)
                            return remote
                        staging = remote + ".part-" + uuid.uuid4().hex
                        with sftp.open(staging, "wb") as handle:
                            for offset in range(0, len(data), 16384):
                                if stop.is_set():
                                    raise CommandError("监视启动已取消。")
                                handle.write(data[offset:offset + 16384])
                        if not verified(staging):
                            raise CommandError("板端采样程序校验失败。")
                        sftp.chmod(staging, 0o755)
                        if stop.is_set():
                            raise CommandError("监视启动已取消。")
                        try:
                            sftp.posix_rename(staging, remote)
                        except OSError:
                            _sftp_remove_quietly(sftp, remote)
                            sftp.rename(staging, remote)
                        staging = None
                    finally:
                        if staging is not None and not stop.is_set():
                            _sftp_remove_quietly(sftp, staging)
                path = remote
            else:
                self.log("SYSTEM", f"板子架构为 {arch or '未知'}，监视改用 devmem 循环。")
        except Exception as exc:
            if stop.is_set():
                raise CommandError("监视启动已取消。") from exc
            self.log("SYSTEM", f"无法放置板端常驻采样程序（{exc}），监视改用 devmem 循环。")
        return path

    def start_monitor(self, addresses, interval_ms, on_samples, stopped, cancel_event, on_sampler=None):
        """Sample registers on the board on a dedicated exec channel (the shell stays free).

        on_samples(list of (seconds, values)) is called about every 40 ms from a reader
        thread; stopped(message) fires once at the end ('' when cancelled); on_sampler(kind)
        reports the board-side sampler ('regmon' or 'shell') once its banner arrives."""
        stop = cancel_event
        if not self.alive:
            raise CommandError("请先连接设备。")
        monitor_script(addresses, interval_ms)   # validate before touching the board
        try:
            sampler = self.monitor_sampler(stop)
        except CommandError:
            if stop.is_set():
                return False
            raise
        if stop.is_set():
            return False
        try:
            with self._monitor_channel(stop, keep_open=True) as channel:
                channel.set_combine_stderr(True)
                channel.exec_command(monitor_command(addresses, interval_ms, sampler))
        except Exception:
            if stop.is_set():
                return False
            raise
        self.log("CMD", f"监视 {len(addresses)} 个寄存器，间隔 {interval_ms:g} ms"
                 f"（{'板端常驻采样' if sampler else 'devmem 循环'}）：" + " ".join(f"0x{a:08x}" for a in addresses))
        parser = MonitorParser(len(addresses))

        def reader():
            message = ""
            reported = ""
            flushed = started = time.monotonic()
            channel.settimeout(0.05)
            try:
                while not stop.is_set():
                    try:
                        data = channel.recv(65536)
                    except socket.timeout:
                        data = None
                    if data == b"":
                        status = channel.recv_exit_status() if channel.exit_status_ready() else -1
                        message = ("SSH 已断开，监视停止。" if not self.alive else
                                   "板端采样进程已退出" + (f"（状态码 {status}）" if status > 0 else "")
                                   + (f"：{parser.note}" if parser.note else "。"))
                        break
                    if data:
                        parser.feed(data)
                    # Only the first point has a deadline: a later silence (network stall)
                    # pauses the run and the dialog shows it; the board side resumes on its own.
                    if not parser.received and time.monotonic() - started > MONITOR_START_TIMEOUT_S:
                        message = "板端采样首点超时，监视已停止，请检查寄存器地址和板端状态。"
                        break
                    if parser.sampler != reported:
                        reported = parser.sampler
                        if sampler and reported == "shell":
                            self.log("SYSTEM", "板端常驻采样程序无法执行，已改用 devmem 循环"
                                     + (f"：{parser.note}" if parser.note else "。"))
                            parser.note = ""
                        if on_sampler:
                            on_sampler(reported)
                    if parser.samples and time.monotonic() - flushed >= 0.04:
                        on_samples(parser.take())
                        flushed = time.monotonic()
                if parser.samples:
                    on_samples(parser.take())
            except Exception as exc:
                message = f"监视连接中断：{exc}"
            finally:
                channel.close()
                stopped("" if stop.is_set() else message)
        threading.Thread(target=reader, daemon=True, name="register-monitor").start()
        return True

    def close(self):
        self._generation += 1
        self.stop_stream()
        # Break a handshake still waiting inside client.connect: a socket
        # shutdown unblocks the recv even on Windows (close alone does not),
        # so the blocked call raises instead of waiting out the timeout.
        sock, self._connect_sock = self._connect_sock, None
        if sock is not None:
            for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
                try:
                    action()
                except OSError:
                    pass
        channel, client = self.chan, self.client
        self.chan = self.client = None
        if channel:
            channel.close()
        if client:
            client.close()


class DemoSession:
    """Explicit offline simulator for inspection and reproducible acceptance tests."""
    def __init__(self, log=lambda level, text: None):
        self.log = log
        self.alive = False
        self.last_exit = 0
        self.memory = {}
        self._stream_stop = threading.Event()
        # fake remote filesystem root, seeded with a demo sunny.log
        import tempfile as _tempfile
        self.remote_root = Path(_tempfile.mkdtemp(prefix="demo-board-"))
        (self.remote_root / "run" / "media" / "sda").mkdir(parents=True, exist_ok=True)
        demo_log = self.remote_root / "run" / "media" / "sda" / "sunny.log"
        if not demo_log.exists():
            demo_log.write_text("\n".join(
                f"2026-09-14 20:{minute:02d}:{second:02d} INFO  [DEMO] board boot ok, fpga loaded"
                .replace("fpga loaded", f"event #{minute * 60 + second}")
                for minute in range(0, 2) for second in range(0, 60, 7)), encoding="utf-8")

    def _to_remote(self, remote_path):
        return self.remote_root / remote_path.lstrip("/")

    def upload_bitfile(self, local_path, remote_dir="/run/media/sda",
                       dest_name="sunny_fpga.bit", timestamp=None, progress=None):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        stamp = timestamp or time.strftime("%Y%m%d%H%M%S")
        target_dir = self._to_remote(remote_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / dest_name
        backup_name = None
        if dest.exists():
            backup_name = f"{dest_name}_{stamp}"
            self.log("CMD", f"rename {remote_dir}/{dest_name} -> {backup_name}")
            dest.rename(target_dir / backup_name)
        total = os.path.getsize(local_path)
        self.log("CMD", f"put {local_path} -> {remote_dir}/{dest_name}")
        chunk = max(1, total // 20)
        written = 0
        with open(local_path, "rb") as src, open(dest, "wb") as dst:
            while True:
                block = src.read(chunk)
                if not block:
                    break
                dst.write(block)
                written += len(block)
                if progress:
                    progress(min(written, total), total)
                time.sleep(0.03)   # visible progress in the UI
        if progress:
            progress(total, total)
        self.log("INFO", f"{remote_dir}/{dest_name}: {total} 字节")
        return backup_name

    def list_bit_backups(self, remote_dir="/run/media/sda", bitname="sunny_fpga.bit"):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        target_dir = self._to_remote(remote_dir)
        if not target_dir.is_dir():
            raise CommandError(f"板端目录不存在：{remote_dir}")
        prefix = bitname + "_"
        names = [p.name for p in target_dir.iterdir()
                 if p.name == bitname or p.name.startswith(prefix)]
        entries = [{"name": bitname, "timestamp": None}] if bitname in names else []
        backups = sorted(({"name": n, "timestamp": n[len(prefix):]} for n in names
                          if n.startswith(prefix)), key=lambda item: item["timestamp"], reverse=True)
        return entries + backups

    def list_dirs(self, parent="/run/media"):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        folder = self._to_remote(parent)
        if not folder.is_dir():
            return []
        return sorted(f"{parent.rstrip('/')}/{path.name}" for path in folder.iterdir() if path.is_dir())

    def rollback_bit(self, backup_name, remote_dir="/run/media/sda",
                     dest_name="sunny_fpga.bit", timestamp=None, progress=None):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        target_dir = self._to_remote(remote_dir)
        dest = target_dir / dest_name
        backup = target_dir / backup_name
        if backup_name == dest_name or not backup_name.startswith(dest_name + "_"):
            raise CommandError("请选择带时间戳的 bit 备份。")
        if not backup.is_file():
            raise CommandError(f"备份文件不存在：{backup_name}")
        stamp = timestamp or time.strftime("%Y%m%d%H%M%S")
        aside = target_dir / f"{dest_name}_{stamp}"
        if dest.exists():
            self.log("CMD", f"rename {dest_name} -> {dest_name}_{stamp}")
            dest.rename(aside)
        if progress:
            progress(50, 100)
        self.log("CMD", f"rename {backup_name} -> {dest_name}")
        backup.rename(dest)
        if progress:
            progress(100, 100)
        self.log("INFO", f"回退完成：{backup_name} -> {dest_name}")

    def download_file(self, remote_path, local_path, progress=None):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        source = self._to_remote(remote_path)
        if not source.is_file():
            raise CommandError(f"演示板端文件不存在：{remote_path}")
        total = source.stat().st_size
        self.log("CMD", f"get {remote_path} -> {local_path}")
        chunk = max(1, total // 20)
        written = 0
        with _atomic_download_target(local_path) as temporary:
            with open(source, "rb") as src, open(temporary, "wb") as dst:
                while True:
                    block = src.read(chunk)
                    if not block:
                        break
                    dst.write(block)
                    written += len(block)
                    if progress:
                        progress(min(written, total), total)
                    time.sleep(0.03)
            if progress:
                progress(total, total)
        self.log("INFO", f"{local_path}: {os.path.getsize(local_path)} 字节")

    def connect(self, *args, **kwargs):
        time.sleep(0.08)
        self.alive = True

    def read(self, address, quiet=False):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        command = read_command(address)
        if not quiet:
            self.log("CMD", command)
        time.sleep(0.008)
        if address not in self.memory:
            offset = address & 0x1FF
            self.memory[address] = {0: 0x00140201, 4: 0x08000000, 8: 1, 0x68: 0x03080100,
                                    0x90: 0x02040000, 0xB8: 0x01020000, 0x178: 125840}.get(offset, 0)
        if not quiet:
            self.log("INFO", f"0x{self.memory[address]:08X}")
        return self.memory[address]

    def write(self, address, width, value, quiet=False):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        command = write_command(address, width, value)
        if not quiet:
            self.log("CMD", command)
        self.memory[address] = value
        return self.read(address, quiet=quiet)

    def read_many(self, addresses):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        addresses = list(addresses)
        if not addresses:
            return {}
        batch = len(addresses) > 1
        if batch:
            address_list = " ".join(f"0x{a:08x}" for a in addresses)
            self.log("CMD", f"读取 {len(addresses)} 个寄存器 {address_list}")
        results = {}
        for address in addresses:
            try:
                results[address] = (self.read(address, quiet=batch), None)
            except Exception as exc:
                results[address] = (None, str(exc))
        if batch:
            failed = [f"0x{a:08X}" for a, (value, error) in results.items() if error]
            if failed:
                self.log("ERROR", f"读取 {len(results) - len(failed)}/{len(results)} 成功，失败：{'、'.join(failed)}")
            else:
                values = "  ".join(f"0x{a:08x}=0x{results[a][0]:08X}" for a in addresses)
                self.log("INFO", values)
        return results

    def run(self, command, timeout=8, quiet=False):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        parts = command.strip().split()
        if len(parts) in (2, 4) and parts[0] == "devmem":
            address = parse_int(parts[1])
            if address is None:
                raise ValueError("地址格式无效。")
            if len(parts) == 2:
                value = self.read(address, quiet=quiet)
            else:
                data = parse_int(parts[3])
                if data is None:
                    raise ValueError("写入值格式无效。")
                value = self.write(address, int(parts[2]), data, quiet=quiet)
            return f"0x{value:08X}"
        if not quiet:
            self.log("CMD", command)
        result = "[演示] 命令已接收；实际 shell 命令需连接设备后执行。"
        if not quiet:
            self.log("INFO", result)
        return result

    def start_stream(self, path, output, stopped, cancel_event=None):
        stop = cancel_event if cancel_event is not None else threading.Event()
        if stop.is_set():
            return False
        self.stop_stream()
        self._stream_stop = stop
        self.log("CMD", "tail -f " + shlex.quote(path))
        def reader():
            stamp = time.strftime('%H:%M:%S')
            output("\n".join(f"{stamp} {line}" for line in (
                "INFO    [DEMO] 日志监听已启动，以下为离线模拟内容",
                "DEBUG   [DEMO] axis status addr=0xB0100808 value=0x00000001",
                "SUCCESS [DEMO] axis 初始化完成，寄存器回读一致",
                "WARN    [DEMO] axis 采样周期偏长，正在观察下一周期",
                "ERROR   [DEMO] 模拟通信超时，仅用于日志高亮预览",
                "INFO    [DEMO] 模拟通信已恢复，继续接收日志",
            )) + "\n")
            count = 0
            while not stop.is_set():
                output(f"{time.strftime('%H:%M:%S')} [DEMO] cycle={count:04d} axis=ready bus=idle\n")
                count += 1
                stop.wait(1)
            stopped()
        threading.Thread(target=reader, daemon=True).start()
        return True

    def stop_stream(self):
        self._stream_stop.set()

    def start_monitor(self, addresses, interval_ms, on_samples, stopped, cancel_event, on_sampler=None):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        addresses = list(addresses)
        monitor_script(addresses, interval_ms)   # same validation as the board path
        stop = cancel_event
        self.log("CMD", f"监视 {len(addresses)} 个寄存器，间隔 {interval_ms:g} ms（演示）")
        bases = [self.read(address, quiet=True) for address in addresses]

        def reader():
            if on_sampler:
                on_sampler("demo")
            period = interval_ms / 1000
            start = stamp = time.time()
            batch, flushed = [], time.monotonic()
            while not stop.is_set() and self.alive:
                phase = stamp - start
                batch.append((stamp, tuple(
                    (base + int(800 * (1 + math.sin(2 * math.pi * phase / 2 + index)))) & 0xFFFFFFFF
                    for index, base in enumerate(bases))))
                if time.monotonic() - flushed >= 0.04:
                    on_samples(batch)
                    batch, flushed = [], time.monotonic()
                stamp += period
                stop.wait(max(0.0, stamp - time.time()))
            if batch:
                on_samples(batch)
            stopped("" if stop.is_set() else "演示会话已关闭。")
        threading.Thread(target=reader, daemon=True, name="demo-monitor").start()
        return True

    def close(self):
        self.stop_stream()
        self.alive = False


def session_logger(directory: Path):
    logger = logging.getLogger("devmem-studio.session")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(directory / "session.log", maxBytes=2 * 1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
    return logger
