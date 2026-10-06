"""Measure real Windows GUI launch-to-responsive-window time on a private desktop.

Each executable is staged with sanitized settings and isolated LOCALAPPDATA/TEMP.
The first measurement is reported separately; it is not claimed to be an OS-cold
launch. Repeated median excludes it. No production settings or windows are touched.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time


class StartupInfo(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


def measure(executable, workdir, environment, sequence):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user32.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
    user32.CreateDesktopW.restype = w.HANDLE
    user32.CloseDesktop.argtypes = [w.HANDLE]
    user32.EnumDesktopWindows.argtypes = [w.HANDLE, callback_type, w.LPARAM]
    user32.IsWindowVisible.argtypes = [w.HWND]
    user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM,
                                         w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL,
                                     w.DWORD, ctypes.c_void_p, w.LPCWSTR,
                                     ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)]
    kernel.CreateProcessW.restype = w.BOOL
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    name = f"DevmemStartup_{os.getpid()}_{sequence}"
    desktop = user32.CreateDesktopW(name, None, None, 0, 0x10000000, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    startup = StartupInfo()
    startup.cb = ctypes.sizeof(startup)
    startup.lpDesktop = name
    process = ProcessInfo()
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable)]))
    env = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(environment.items())) + "\0\0")
    hwnd = None
    try:
        start = time.perf_counter()
        if not kernel.CreateProcessW(str(executable), command, None, None, False, 0x08000400,
                                     env, str(workdir), ctypes.byref(startup), ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        while time.perf_counter() - start < 40:
            windows = []
            @callback_type
            def visit(handle, _):
                if user32.IsWindowVisible(handle):
                    title = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(handle, title, len(title))
                    if title.value.startswith("DevmemStudio"):
                        windows.append(handle)
                        return False
                return True
            user32.EnumDesktopWindows(desktop, visit, 0)
            if windows:
                hwnd = windows[0]
                reply = ctypes.c_size_t()
                if user32.SendMessageTimeoutW(hwnd, 0, 0, 0, 2, 100, ctypes.byref(reply)):
                    elapsed = time.perf_counter() - start
                    break
            if kernel.WaitForSingleObject(process.hProcess, 0) == 0:
                code = w.DWORD()
                kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(code))
                raise RuntimeError(f"Application exited before ready: {code.value}")
            time.sleep(0.005)
        else:
            raise TimeoutError("Application did not expose a responsive main window within 40s")
        user32.PostMessageW(hwnd, 0x0010, 0, 0)
        if kernel.WaitForSingleObject(process.hProcess, 15000) != 0:
            raise TimeoutError("Benchmark application did not exit cleanly")
        code = w.DWORD()
        kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(code))
        if code.value:
            raise RuntimeError(f"Benchmark application exit code: {code.value}")
        return elapsed
    finally:
        if process.hProcess and kernel.WaitForSingleObject(process.hProcess, 0) != 0:
            if hwnd:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
            if kernel.WaitForSingleObject(process.hProcess, 3000) != 0:
                # Only the explicitly created benchmark instance is eligible.
                kernel.TerminateProcess(process.hProcess, 1)
                kernel.WaitForSingleObject(process.hProcess, 3000)
        if process.hThread:
            kernel.CloseHandle(process.hThread)
        if process.hProcess:
            kernel.CloseHandle(process.hProcess)
        user32.CloseDesktop(desktop)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runs", type=int, default=5, help="Repeated launches in addition to the first")
    parser.add_argument("--top", type=Path)
    args = parser.parse_args()
    if args.runs < 1:
        raise ValueError("At least one repeated launch is required")
    source = args.exe.resolve()
    destination = args.output.resolve()
    root = Path(__file__).resolve().parents[1]
    artifacts = (root / "artifacts").resolve()
    if destination == artifacts or not destination.is_relative_to(artifacts):
        raise ValueError("Benchmark output must be inside this project's artifacts directory")
    appdir = destination / "app"
    appdir.mkdir(parents=True, exist_ok=True)
    executable = appdir / "DevmemStudio.exe"
    shutil.copy2(source, executable)
    if (source.parent / "_internal").is_dir():
        shutil.copytree(source.parent / "_internal", appdir / "_internal", dirs_exist_ok=True)
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "QT_QPA_PLATFORM", "QT_SCALE_FACTOR",
                "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(key, None)
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    env["DEVMEMSTUDIO_IGNORE_OVERRIDES"] = "1"
    env["LOCALAPPDATA"] = str(destination / "localappdata")
    env["TEMP"] = env["TMP"] = str(destination / "temporary-runtime")
    Path(env["TEMP"]).mkdir(parents=True, exist_ok=True)
    settings = {"host": "", "password": "", "remember_password": False, "window_size": [1280, 800]}
    if args.top:
        settings["top_path"] = str(args.top.resolve())
    (appdir / "registers.json").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    seconds = [measure(executable, appdir, env, i) for i in range(args.runs + 1)]
    report = {"executable": str(source), "bytes": source.stat().st_size,
              "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "saved_top": bool(args.top),
              "metric": "CreateProcess to visible main window responding to WM_NULL",
              "first_launch_seconds": seconds[0], "repeat_seconds": seconds[1:],
              "repeat_median_seconds": statistics.median(seconds[1:]),
              "repeat_min_seconds": min(seconds[1:]), "private_desktop": True,
              "production_settings_untouched": True, "os_cold_cache_not_claimed": True}
    (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
