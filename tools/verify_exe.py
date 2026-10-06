"""Validate the single-file EXE copied alone, without Python/venv and with an empty per-user runtime cache.

Run 1 unpacks the runtime and passes the offline acceptance; run 2 starts from a damaged cache plus a
stale old runtime and must repair the one, remove the other and pass again.
"""
import argparse
import ctypes
from ctypes import wintypes as w
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--output-name", default="exe-isolated")
parser.add_argument("--executable", type=Path)
parser.add_argument("--native", action="store_true")
parser.add_argument("--scale", default="1")
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from devmem_studio import __version__

artifacts = (root / "artifacts").resolve()
target = (artifacts / args.output_name).resolve()
if not target.is_relative_to(artifacts) or target == artifacts:
    raise ValueError("Output must remain inside the project's artifacts directory")
if target.exists():
    shutil.rmtree(target)
target.mkdir(parents=True)
executable = target / "DevmemStudio.exe"
source = (args.executable or root / "dist" / "DevmemStudio.exe").resolve()
shutil.copy2(source, executable)   # the EXE alone: nothing else may be needed beside it
local = target / "localappdata"
env = os.environ.copy()
for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_PLATFORM"):
    env.pop(key, None)
env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
env["QT_SCALE_FACTOR"] = args.scale
env["LOCALAPPDATA"] = str(local)
env["TEMP"] = env["TMP"] = str(target / "temp")
Path(env["TEMP"]).mkdir()
runtime_base = local / "DevmemStudio" / "runtime"


def accept(run):
    output = target / f"acceptance-{run}"
    command = [str(executable), "--smoke-test", str(output)] + ([] if args.native else ["--offscreen"])
    started = time.perf_counter()
    result = subprocess.run(command, cwd=target, env=env, capture_output=True, timeout=180,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    seconds = time.perf_counter() - started
    (target / f"stdout-{run}.log").write_bytes(result.stdout)
    (target / f"stderr-{run}.log").write_bytes(result.stderr)
    report_path = output / "report.json"
    if result.returncode or not report_path.exists():
        raise RuntimeError(f"EXE acceptance run {run} failed (exit {result.returncode}). See {target}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report["passed"] or not report["frozen"]:
        raise RuntimeError(f"EXE acceptance failed: {report_path}")
    if report.get("paramiko") != version("paramiko"):
        raise RuntimeError(f"The EXE SSH library does not match the project venv: {report_path}")
    return report, seconds


def runtimes():
    return sorted(item.name for item in runtime_base.iterdir()) if runtime_base.is_dir() else []


def age_directory(path, hours):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.SetFileTime.argtypes = [w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.c_void_p, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x100, 7, None, 3, 0x02000000, None)   # FILE_WRITE_ATTRIBUTES, backup semantics
    if handle in (None, w.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    stamp = int((time.time() - hours * 3600 + 11644473600) * 10_000_000)
    try:
        if not kernel.SetFileTime(handle, ctypes.byref(w.FILETIME(stamp & 0xFFFFFFFF, stamp >> 32)), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


first, first_seconds = accept(1)
cached = runtimes()
if len(cached) != 1 or not cached[0].startswith(f"{__version__}-"):
    raise RuntimeError(f"Expected one unpacked runtime for {__version__}, found {cached}")
cache = runtime_base / cached[0]
if not (cache / "DevmemStudio.exe").is_file() or any(cache.rglob("registers.json")):
    raise RuntimeError(f"Unexpected runtime cache content in {cache}")
damaged = cache / "_internal" / "base_library.zip"
expected_size = damaged.stat().st_size
with damaged.open("ab") as handle:
    handle.write(b"damage")
stale = runtime_base / "0.0.0-stale"
(stale / "_internal").mkdir(parents=True)
(stale / "_internal" / "leftover.bin").write_bytes(b"x" * 1024)
age_directory(stale, hours=2)
second, second_seconds = accept(2)
if runtimes() != cached or damaged.stat().st_size != expected_size:
    raise RuntimeError(f"Cache repair or stale-runtime cleanup failed: {runtimes()}")
print(json.dumps({"passed": True, "single_file": True, "exe_bytes": executable.stat().st_size,
                  "checks": len(second["checks"]), "paramiko": second["paramiko"],
                  "runtime": cached[0], "first_run_seconds": round(first_seconds, 1),
                  "repaired_run_seconds": round(second_seconds, 1), "damaged_cache_repaired": True,
                  "stale_runtime_removed": True, "python_path_removed": True, "native": args.native,
                  "scale": args.scale, "report": str(target / "acceptance-2" / "report.json")}, ensure_ascii=False))
