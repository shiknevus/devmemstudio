# -*- coding: utf-8 -*-
"""Configuration, register semantics and SSH transport; independent of the GUI."""
from __future__ import annotations

import base64
import codecs
import copy
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import threading
import time
import uuid

import paramiko

from .catalog import DEFAULT_CATEGORIES, DEFAULT_TYPES, NAME_GROUPS, REGISTER_FIELDS

DEFAULT_LOG_PATH = "/run/media/sda/sunny.log"


def application_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


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


def parse_devmem_read_output(text, addr_val=None):
    """Accept a value line or 'Value at address (...) : / = value'; reject echoes/errors."""
    if not text:
        return None, None
    candidates = []
    number = r"(0[xX][0-9a-fA-F]+|[0-9]+)"
    for line in strip_ansi(text).splitlines():
        line = line.strip()
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


def default_config() -> dict:
    return {"host": "", "port": 22, "username": "root", "password": "",
            "base": "0xb0100000", "default_width": 32, "last_category": "axis",
            "last_address": "0x0800", "connect_timeout": 2, "remember_password": False,
            "top_path": "", "poll_interval": 1000, "log_path": DEFAULT_LOG_PATH, "write_cache": {}}


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
        for field, fallback, low, high in (("port", 22, 1, 65535), ("connect_timeout", 2, 1, 120),
                                            ("poll_interval", 1000, 250, 60000)):
            val = parse_int(cfg.get(field))
            cfg[field] = val if val is not None and low <= val <= high else fallback
        for field in ("base", "last_address"):
            try:
                validated_address(cfg[field])
            except (ValueError, TypeError):
                cfg[field] = default_config()[field]
        for field in ("host", "username", "password", "log_path", "top_path"):
            cfg[field] = str(cfg.get(field) or "")
        if not isinstance(cfg.get("write_cache"), dict):
            cfg["write_cache"] = {}
        return cfg

    def save(self, cfg: dict):
        data = copy.deepcopy(cfg)
        data.pop("types", None)
        data.pop("categories", None)
        if not data.get("remember_password"):
            data["password"] = ""
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


class HostKeyChangedError(paramiko.BadHostKeyException):
    """A specific observed host key change, retained for explicit confirmation."""
    def __init__(self, hostname, port, key, expected_key):
        super().__init__(hostname, key, expected_key)
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

    @property
    def alive(self):
        try:
            return bool(self.client and self.client.get_transport() and self.client.get_transport().is_active()
                        and self.chan and not self.chan.closed)
        except Exception:
            return False

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
                client.connect(hostname=host, port=port, username=username, password=password,
                               timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                               channel_timeout=timeout, allow_agent=False, look_for_keys=False,
                               transport_factory=BoardTransport)
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
        except Exception:
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

    def run(self, command: str, timeout=8):
        if not command.strip() or "\x00" in command:
            raise ValueError("命令不能为空或包含空字符。")
        with self._lock:
            if not self.alive:
                raise CommandError("SSH 已断开，请重新连接设备。")
            channel = self.chan
            self.last_exit = None
            while channel.recv_ready():
                channel.recv(65536)
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
                        lines = decoded[:match.start()].splitlines()
                        lines = [line for line in lines if marker not in line and line.strip() != command.strip()
                                 and not re.fullmatch(r"[^\n]*[#$>]\s*", line)]
                        result = "\n".join(lines).strip()
                        if result:
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

    def read(self, address: int) -> int:
        output = self.run(read_command(address))
        value, _ = parse_devmem_read_output(output, address)
        if value is None:
            raise CommandError("未识别到寄存器数值：" + (output[:240] or "设备未返回数据"))
        return value

    def write(self, address: int, width: int, value: int) -> int:
        self.run(write_command(address, width, value))
        # Readback is observed data; self-clearing/action registers need not equal the write value.
        try:
            return self.read(address)
        except Exception as exc:
            raise ReadbackError(f"0x{address:08X} 写入已完成，但回读失败：{exc}") from exc

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
        # Publish the pending channel so closing the log window can cancel an
        # exec request without closing the register shell or its SSH transport.
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

    def upload_bitfile(self, local_path, remote_dir="/run/media/sda",
                       dest_name="sunny_fpga.bit", timestamp=None, progress=None):
        """Rename the existing remote bit to a timestamped backup, then upload the new one.

        The remote directory and any parent path components are created on demand; the
        previous sunny_fpga.bit (if present) is renamed to sunny_fpga.bit_<timestamp>.
        Runs on the worker thread; `progress(done, total)` reports byte transfer."""
        if not self.alive:
            raise CommandError("请先连接设备。")
        import os
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
            backup_name = f"{dest_name}_{stamp}"
            backup_path = f"{remote_dir.rstrip('/')}/{backup_name}"
            try:
                sftp.stat(f"{remote_dir}/{dest_name}")
                self.log("CMD", f"rename {remote_dir}/{dest_name} -> {backup_name}")
                sftp.rename(f"{remote_dir}/{dest_name}", backup_path)
            except IOError:
                pass  # no previous bit to back up
            total = os.path.getsize(local_path)
            sent = [0]

            def callback(transferred, _total):
                if progress is not None and transferred > sent[0]:
                    sent[0] = transferred
                    progress(transferred, total)

            remote_path = f"{remote_dir}/{dest_name}"
            self.log("CMD", f"put {local_path} -> {remote_path}")
            sftp.put(local_path, remote_path, callback=callback)
            try:
                attrs = sftp.stat(remote_path)
                self.log("INFO", f"{remote_path}: {attrs.st_size} 字节")
            except IOError:
                pass
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
            try:
                sftp.stat(dest)
                self.log("CMD", f"rename {dest_name} -> {dest_name}_{stamp}")
                sftp.rename(dest, aside)
            except IOError:
                pass  # no current bit to move aside
            if progress:
                progress(50, 100)
            self.log("CMD", f"rename {backup_name} -> {dest_name}")
            sftp.rename(backup, dest)
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
        import os
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
            sftp.get(remote_path, local_path, callback=callback)
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

    def close(self):
        self._generation += 1
        self.stop_stream()
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
                .replace("20:", "20:").replace("fpga loaded", f"event #{minute * 60 + second}")
                for minute in range(0, 2) for second in range(0, 60, 7)), encoding="utf-8")

    def _to_remote(self, remote_path):
        return self.remote_root / remote_path.lstrip("/")

    def upload_bitfile(self, local_path, remote_dir="/run/media/sda",
                       dest_name="sunny_fpga.bit", timestamp=None, progress=None):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        import os
        stamp = timestamp or time.strftime("%Y%m%d%H%M%S")
        target_dir = self._to_remote(remote_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / dest_name
        if dest.exists():
            backup = target_dir / f"{dest_name}_{stamp}"
            self.log("CMD", f"rename {remote_dir}/{dest_name} -> {dest.name}_{stamp}")
            dest.rename(backup)
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
        import os
        source = self._to_remote(remote_path)
        if not source.is_file():
            raise CommandError(f"演示板端文件不存在：{remote_path}")
        total = source.stat().st_size
        self.log("CMD", f"get {remote_path} -> {local_path}")
        chunk = max(1, total // 20)
        written = 0
        with open(source, "rb") as src, open(local_path, "wb") as dst:
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

    def read(self, address):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        self.log("CMD", read_command(address))
        time.sleep(0.008)
        if address not in self.memory:
            offset = address & 0x1FF
            self.memory[address] = {0: 0x00140201, 4: 0x08000000, 8: 1, 0x68: 0x03080100,
                                    0x90: 0x02040000, 0xB8: 0x01020000, 0x178: 125840}.get(offset, 0)
        self.log("INFO", f"0x{self.memory[address]:08X}")
        return self.memory[address]

    def write(self, address, width, value):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        self.log("CMD", write_command(address, width, value))
        self.memory[address] = value
        return self.read(address)

    def run(self, command, timeout=8):
        if not self.alive:
            raise CommandError("演示会话已关闭。")
        parts = command.strip().split()
        if len(parts) in (2, 4) and parts[0] == "devmem":
            address = parse_int(parts[1])
            if address is None:
                raise ValueError("地址格式无效。")
            value = self.read(address) if len(parts) == 2 else self.write(address, int(parts[2]), parse_int(parts[3]))
            return f"0x{value:08X}"
        self.log("CMD", command)
        result = "[演示] 命令已接收；实际 shell 命令需连接设备后执行。"
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
