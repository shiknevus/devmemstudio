"""Bundled safe packing engine and cooperative, per-job Windows cancellation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

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
        self._stopped = threading.Event()
        self._handle = self._kernel.CreateEventW(None, True, False, self.name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def cancel(self):
        if self._handle and not self._kernel.SetEvent(self._handle):
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())
        self._stopped.set()

    def is_set(self):
        return self._stopped.is_set()

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
    result["include_bit"] = raw.get("include_bit") is not False
    result["include_bin"] = raw.get("include_bin") is True
    entries = raw.get("bins", {})
    result["bins"] = {path: {"checked": entry.get("checked") is True,
                              "version": entry.get("version", "")[:63]}
                      for path, entry in entries.items()
                      if isinstance(path, str) and isinstance(entry, dict)
                      and isinstance(entry.get("version", ""), str)} if isinstance(entries, dict) else {}
    return result


def scan_firmware_files(directory: Path, cancel, progress) -> dict:
    """Find bit/bin files in a cancellable walk without following junctions."""
    files, bins = [], []
    pending = [(Path(directory), 0)]
    folders = skipped = 0
    truncated = False
    while pending:
        if cancel.is_set():
            return {"code": 3}
        folder, depth = pending.pop()
        folders += 1
        if folders > 20000:
            truncated = True
            break
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if cancel.is_set():
                        return {"code": 3}
                    try:
                        if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                            skipped += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.startswith("."):
                                continue
                            if depth >= 32:
                                skipped += 1
                                truncated = True
                            else:
                                pending.append((Path(entry.path), depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            suffix = Path(entry.name).suffix.lower()
                            if suffix in (".bit", ".bin"):
                                (files if suffix == ".bit" else bins).append(entry.path)
                                if len(files) + len(bins) >= 2000:
                                    truncated = True
                                    pending.clear()
                                    break
                    except OSError:
                        skipped += 1
        except OSError:
            skipped += 1
    return {"code": 0, "files": sorted(files, key=str.casefold), "bins": sorted(bins, key=str.casefold),
            "skipped": skipped, "truncated": truncated}


def firmware_members(source, bins):
    """Validate selection and map it to unique names inside the ZIP."""
    members = []
    if source is not None:
        source = Path(source)
        if source.suffix.lower() != ".bit" or not source.is_file():
            raise ValueError("请选择存在的 .bit 文件。")
        members.append((source, "sunny_fpga.bit"))
    versions = set()
    for raw_path, raw_version in bins:
        path, version = Path(raw_path), raw_version.strip()
        if path.suffix.lower() != ".bin" or not path.is_file():
            raise ValueError(f"bin 文件不存在：{path}")
        if len(bins) > 1 and not version:
            raise ValueError("选择多个 bin 时，请为每个 bin 填写不同的版本号。")
        if version:
            if (len(version) > 63 or version in (".", "..") or version.endswith((".", " "))
                    or re.search(r'[\\/:*?"<>|\x00-\x1f]', version)
                    or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", version)):
                raise ValueError("版本号须为有效的子文件夹名，不能包含路径分隔符或特殊字符。")
            if version.casefold() in versions:
                raise ValueError("每个 bin 的版本号必须不同（不区分大小写）。")
            versions.add(version.casefold())
        members.append((path, f"{version}/{path.name}" if version else path.name))
    if not members:
        raise ValueError("请勾选需要打包的 bit 或 bin 文件。")
    names = [name.casefold() for _, name in members]
    if len(names) != len(set(names)):
        raise ValueError("包内文件名重复，请填写不同的版本号。")
    return members


class _PackStopped(Exception):
    def __init__(self, code):
        self.code = code


def run_firmware_pack(source, bins, output, project, timeout, cancel, progress):
    """Stream selected firmware to one verified ZIP and publish only on success."""
    members = firmware_members(source, bins)
    if not project or len(project) > 63 or re.search(r'[\s\\/:*?"<>|\x00-\x1f]', project):
        raise ValueError("项目号无效。")
    output = Path(output)
    deadline = time.monotonic() + timeout
    temporary = None

    def check():
        if cancel.is_set():
            raise _PackStopped(3)
        if time.monotonic() >= deadline:
            raise _PackStopped(4)

    try:
        check()
        with tempfile.NamedTemporaryFile(dir=output, prefix=".bitpack-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
        digests = {}
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path, name in members:
                check()
                progress(f"正在打包：{name}")
                digest = hashlib.sha256()
                with path.open("rb") as incoming, archive.open(name, "w", force_zip64=True) as packed:
                    while chunk := incoming.read(1024 * 1024):
                        check()
                        digest.update(chunk)
                        packed.write(chunk)
                digests[name] = digest.digest()
        progress("正在校验 ZIP 文件…")
        with zipfile.ZipFile(temporary) as archive:
            for name, expected in digests.items():
                digest = hashlib.sha256()
                with archive.open(name) as incoming:
                    while chunk := incoming.read(1024 * 1024):
                        check()
                        digest.update(chunk)
                if digest.digest() != expected:
                    raise OSError(f"ZIP 校验失败：{name}")
        check()
        kind = "bit_bin" if source is not None and bins else "bit" if source is not None else "bin"
        stem = f"{project}_{kind}_{time.strftime('%Y%m%d_%H%M%S')}"
        number = 1
        while True:
            check()
            target = output / f"{stem}{'' if number == 1 else '_' + str(number)}.zip"
            try:
                # Windows rename refuses existing targets and also supports FAT
                # and network output folders. POSIX uses a no-replace hard link.
                if os.name == "nt":
                    os.rename(temporary, target)
                else:
                    os.link(temporary, target)
                break
            except FileExistsError:
                number += 1
        progress(f"校验通过：{target}")
        return {"code": 0, "path": str(target)}
    except _PackStopped as exc:
        return {"code": exc.code, "path": None}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
