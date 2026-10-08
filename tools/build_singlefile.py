"""Pack the PyInstaller runtime directory into one small native EXE.

The payload is compressed with Windows' built-in LZMS codec (no decoder shipped) as a few
independent blocks, packed and unpacked in parallel; the launcher unpacks it once per
version into %LOCALAPPDATA%\\DevmemStudio\\runtime.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes as w
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from devmem_studio import __version__

LZMS, BLOCK_SIZE_INFO, MAX_BLOCK = 5, 1, 64 << 20
BLOCKS = 3   # 74.6 MB runtime: 26 s -> 10 s to pack, +0.8% size; more blocks cost size, not time


def compress_lzms(raw):
    api = ctypes.WinDLL("cabinet", use_last_error=True)
    api.CreateCompressor.argtypes = [w.DWORD, ctypes.c_void_p, ctypes.POINTER(w.HANDLE)]
    api.CreateCompressor.restype = w.BOOL
    api.SetCompressorInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    api.SetCompressorInformation.restype = w.BOOL
    api.Compress.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                             ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    api.Compress.restype = w.BOOL
    api.CloseCompressor.argtypes = [w.HANDLE]
    handle = w.HANDLE()
    if not api.CreateCompressor(LZMS, None, ctypes.byref(handle)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        block = w.DWORD(MAX_BLOCK)   # LZMS caps blocks at 64 MB
        if not api.SetCompressorInformation(handle, BLOCK_SIZE_INFO, ctypes.byref(block), ctypes.sizeof(block)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = ctypes.c_size_t()
        api.Compress(handle, raw, len(raw), None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        result = ctypes.create_string_buffer(size.value)
        if not api.Compress(handle, raw, len(raw), result, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return result.raw[:size.value]
    finally:
        api.CloseCompressor(handle)


def c_wide(text):
    """Body of a C wide literal; non-ASCII as universal character names."""
    return "".join("\\" + ch if ch in '\\"' else ch if " " <= ch <= "~" else f"\\u{ord(ch):04x}" for ch in text)


def collect(runtime):
    records, parts = [], []
    for file in sorted(runtime.rglob("*"), key=lambda item: item.relative_to(runtime).as_posix()):
        if not file.is_file():
            continue
        relative = file.relative_to(runtime).as_posix()
        if file.is_symlink() or ":" in relative or any(part in (".", "..") for part in PurePosixPath(relative).parts):
            raise ValueError(f"Unsafe runtime path: {relative}")
        if relative.lower().endswith("registers.json"):
            raise ValueError(f"Local settings must never be packed: {relative}")
        data = file.read_bytes()
        records.append({"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        parts.append(data)
    if not any(record["path"] == "DevmemStudio.exe" for record in records):
        raise SystemExit(f"{runtime} is not a PyInstaller runtime directory")
    return records, b"".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=ROOT / "build" / "dist" / "_runtime")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "DevmemStudio.exe")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    if not (runtime / "DevmemStudio.exe").is_file() or not (runtime / "_internal").is_dir():
        raise SystemExit("Build the PyInstaller runtime first (DevmemStudio.spec)")
    gcc, windres = (shutil.which(f"x86_64-w64-mingw32-{tool}") or shutil.which(tool) for tool in ("gcc", "windres"))
    if not gcc or not windres:
        raise SystemExit("MinGW-w64 gcc and windres are required to build the single-file launcher")
    if subprocess.run([gcc, "-dumpmachine"], capture_output=True, text=True).stdout.strip().split("-")[0] != "x86_64":
        raise SystemExit(f"{gcc} does not target x86_64; the launcher must match the 64-bit runtime")
    build = ROOT / "build" / "singlefile-launcher"
    build.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    records, raw = collect(runtime)
    content = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    runtime_id = f"{__version__}-{content[:12]}"
    raw_sha = hashlib.sha256(raw).digest()
    payload, payload_key = build / "payload.bin", build / "payload.json"
    try:
        cached = json.loads(payload_key.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}
    reused = payload.is_file() and cached.get("raw_sha256") == raw_sha.hex() and len(cached.get("blocks", ())) == BLOCKS
    if reused:   # unchanged runtime: the LZMS bytes would be identical
        packed, blocks = payload.read_bytes(), cached["blocks"]
    else:
        payload_key.unlink(missing_ok=True)
        step = -(-len(raw) // BLOCKS)
        parts = [raw[offset:offset + step] for offset in range(0, len(raw), step)]
        with ThreadPoolExecutor(len(parts)) as pool:
            packed_parts = list(pool.map(compress_lzms, parts))
        packed = b"".join(packed_parts)
        blocks = [[len(done), len(part)] for done, part in zip(packed_parts, parts)]
        payload.write_bytes(packed)
        payload_key.write_text(json.dumps({"raw_sha256": raw_sha.hex(), "blocks": blocks}), encoding="utf-8")
    stamp = (int(datetime.now(timezone.utc).timestamp()) // 2 * 2 + 11644473600) * 10_000_000   # FILETIME, even second
    digest = ",".join(f"0x{byte:02x}" for byte in raw_sha)
    lines = ["#include <stddef.h>",
             f'#define RUNTIME_ID L"{runtime_id}"',
             f'#define RUNTIME_VERSION L"{__version__}"',
             f"#define RUNTIME_RAW_SIZE ((size_t){len(raw)}u)",
             f"#define RUNTIME_FILE_COUNT {len(records)}",
             f"#define RUNTIME_BLOCK_COUNT {len(blocks)}",
             f"#define RUNTIME_MTIME {stamp}ULL",
             "typedef struct { const wchar_t *path; DWORD size; } RuntimeFile;",
             "typedef struct { DWORD packed; DWORD raw; } RuntimeBlock;   /* consecutive LZMS streams */",
             f"static const unsigned char runtime_sha256[32] = {{{digest}}};",
             "static const RuntimeBlock runtime_blocks[RUNTIME_BLOCK_COUNT] = {",
             *(f"    {{{packed_size}u, {raw_size}u}}," for packed_size, raw_size in blocks),
             "};",
             "static const RuntimeFile runtime_files[RUNTIME_FILE_COUNT] = {"]
    lines += [f'    {{L"{c_wide(record["path"].replace("/", chr(92)))}", {record["size"]}u}},' for record in records]
    lines.append("};")
    (build / "runtime_manifest.h").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (build / "launcher.manifest").write_text("""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
 <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
  <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
 </requestedPrivileges></security></trustInfo>
 <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application>
  <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
 </application></compatibility>
 <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
  <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
  <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
 </windowsSettings></application>
 <dependency><dependentAssembly>
  <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0"
   processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/>
 </dependentAssembly></dependency>
</assembly>
""", encoding="utf-8")
    numbers = ",".join((__version__.split(".") + ["0", "0", "0"])[:4])
    icon = (ROOT / "assets" / "devmem.ico").as_posix()
    (build / "launcher.rc").write_text(f"""#include <windows.h>
101 ICON "{icon}"
1 RT_MANIFEST "launcher.manifest"
501 RCDATA "payload.bin"
1 VERSIONINFO
FILEVERSION {numbers}
PRODUCTVERSION {numbers}
FILEFLAGSMASK 0x3fL
FILEFLAGS 0x0L
FILEOS VOS_NT_WINDOWS32
FILETYPE VFT_APP
BEGIN
 BLOCK "StringFileInfo"
 BEGIN
  BLOCK "040904b0"
  BEGIN
   VALUE "CompanyName", "sunny"
   VALUE "FileDescription", "DevmemStudio"
   VALUE "FileVersion", "{__version__}"
   VALUE "InternalName", "DevmemStudio"
   VALUE "OriginalFilename", "DevmemStudio.exe"
   VALUE "ProductName", "DevmemStudio"
   VALUE "ProductVersion", "{__version__}"
   VALUE "LegalCopyright", "auth szzhang/cgliu/bxli"
  END
 END
 BLOCK "VarFileInfo"
 BEGIN
  VALUE "Translation", 0x409, 1200
 END
END
""", encoding="utf-8")
    subprocess.run([windres, "-c", "65001", "launcher.rc", "-O", "coff", "-o", "launcher.res"], cwd=build, check=True)
    result = build / "DevmemStudio.new.exe"
    subprocess.run([gcc, "-std=c11", "-Os", "-s", "-Wall", "-Wextra", "-Werror", "-municode", "-mwindows",
                    "-fno-ident", "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
                    "-Wl,--dynamicbase,--nxcompat,--high-entropy-va", "-I", str(build),
                    str(ROOT / "tools" / "runtime_launcher" / "launcher.c"), "launcher.res",
                    "-lcabinet", "-lbcrypt", "-o", str(result)], cwd=build, check=True)
    target = args.output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        result.replace(target)
    except PermissionError:
        raise SystemExit(f"{target} is in use; close DevmemStudio and build again")
    report = {"version": __version__, "runtime_id": runtime_id, "files": len(records),
              "runtime_bytes": len(raw), "payload_bytes": len(packed), "payload_blocks": len(blocks), "payload_reused": reused,
              "exe_bytes": target.stat().st_size,
              "exe_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "codec": "Windows LZMS",
              "cache": "%LOCALAPPDATA%\\DevmemStudio\\runtime\\" + runtime_id,
              "build_seconds": round(time.perf_counter() - started, 1)}
    (ROOT / "artifacts").mkdir(exist_ok=True)
    (ROOT / "artifacts" / "singlefile-build.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
