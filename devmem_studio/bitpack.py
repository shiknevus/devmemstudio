"""Bundled safe packing engine and cooperative, per-job Windows cancellation."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from .core import resource_path, user_data_dir

BITPACK_RESOURCE = "assets/bitpack/pack_bit.exe"


def prepare_bitpack_executable() -> Path:
    source = resource_path(BITPACK_RESOURCE)
    if not source.is_file():
        raise FileNotFoundError(f"内置打包工具缺失：{source}。请重新构建或下载完整的工作台 EXE。")
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    directory = user_data_dir() / "tools" / "bitpack" / digest
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "pack_bit.exe"
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
        return target
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix="pack_bit-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


class CancelEvent:
    """A named manual-reset event shared with just this CLI packaging job."""
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Bit_pack 打包引擎仅支持 Windows。")
        import ctypes
        from ctypes import wintypes
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel.CreateEventW.restype = wintypes.HANDLE
        self._kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self._kernel.SetEvent.restype = wintypes.BOOL
        self._kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel.CloseHandle.restype = wintypes.BOOL
        self.name = "Local\\DevmemStudio.BitPack." + uuid.uuid4().hex
        self._handle = self._kernel.CreateEventW(None, True, False, self.name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def cancel(self):
        if self._handle and not self._kernel.SetEvent(self._handle):
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def run_bitpack(source: Path, output: Path, project: str, nopack: bool,
                force: bool, timeout: int, cancel: CancelEvent, progress) -> dict:
    executable = prepare_bitpack_executable()
    arguments = [str(executable), "--input", str(source), "--output", str(output),
                 "--timeout", str(timeout), "--cancel-event", cancel.name, "--progress"]
    if nopack:
        arguments.append("--nopack")
        if force:
            arguments.append("--force")
    else:
        arguments.extend(["--project", project])
    # Windows GUI-subsystem backend writes UTF-8 to the pipe without showing a
    # console or native GUI. Paths remain arguments, never shell/script text.
    lines = []
    with subprocess.Popen(arguments, cwd=str(output), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                          creationflags=subprocess.CREATE_NO_WINDOW) as process:
        for line in process.stdout:
            line = line.rstrip("\r\n")
            lines.append(line)
            progress(line)
        code = process.wait()
    result = None
    if code == 0:
        for index, line in enumerate(lines[:-1]):
            if line.startswith("完成："):
                result = lines[index + 1]
                break
        if not result or not Path(result).is_file():
            raise OSError("打包引擎未返回有效的输出文件，请查看任务日志。")
    return {"code": code, "path": result, "log": "\n".join(lines)}


def normalize_preferences(value) -> dict:
    raw = value if isinstance(value, dict) else {}
    result = {}
    for key in ("project", "source_directory", "output_directory", "source_path"):
        text = raw.get(key, "")
        limit = 63 if key == "project" else 32760
        result[key] = text if isinstance(text, str) and len(text) <= limit else ""
    result["source_mode"] = "folder" if raw.get("source_mode") == "folder" else "file"
    result["nopack"] = raw.get("nopack") is True
    return result


def scan_bitfiles(directory: Path, cancel: CancelEvent, progress) -> dict:
    executable = prepare_bitpack_executable()
    arguments = [str(executable), "--scan", str(directory), "--cancel-event", cancel.name]
    files, messages = [], []
    skipped, truncated = 0, False
    with subprocess.Popen(arguments, cwd=str(directory), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                          creationflags=subprocess.CREATE_NO_WINDOW) as process:
        for raw in process.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith("FILE\t"):
                files.append(line[5:])
            elif line.startswith("SCAN\t"):
                _, count, skip, truncated_flag = line.split("\t")
                skipped, truncated = int(skip), truncated_flag == "1"
            else:
                messages.append(line)
                progress(line)
        code = process.wait()
    return {"code": code, "files": files, "skipped": skipped,
            "truncated": truncated, "log": "\n".join(messages)}
