"""Accept the bundled native Bit_pack popup on an isolated Windows desktop."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "bitpack-gui-acceptance"
OUTPUT.mkdir(parents=True, exist_ok=True)
REPORT = OUTPUT / "report.json"


def write_report(report: dict) -> int:
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if os.name != "nt":
    raise SystemExit(write_report({"passed": True, "skipped": "native Bit_pack GUI requires Windows", "checks": []}))

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.EnumDesktopWindows.argtypes = [wintypes.HANDLE, callback_type, wintypes.LPARAM]
user32.EnumDesktopWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

desktop = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())


def tool_window(pid: int):
    matches = []

    @callback_type
    def visit(handle, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(handle):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(handle, title, len(title))
            if title.value.startswith("Bit_pack"):
                matches.append(handle)
                return False
        return True

    user32.EnumDesktopWindows(desktop, visit, 0)
    return matches[0] if matches else None


def settle(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Native Bit_pack GUI did not settle")


checks = []
process = None
hwnd = None
try:
    sys.path.insert(0, str(ROOT))
    from devmem_studio import bitpack

    # Use the same content-addressed extraction path as the workbench.  This
    # catches stale/missing bundled assets without patching removed MainWindow
    # attributes (the old verifier referenced bit_pack_launcher/application_dir).
    with patch("devmem_studio.bitpack.user_data_dir", return_value=OUTPUT / "cache"):
        executable = bitpack.prepare_bitpack_executable()
    assert executable.is_file()
    checks.append("Bundled Bit_pack executable extracts to its verified cache")
    process = subprocess.Popen([str(executable)], cwd=str(OUTPUT))
    settle(lambda: tool_window(process.pid) is not None)
    hwnd = tool_window(process.pid)
    assert hwnd
    checks.append("Bundled native Bit_pack GUI opens on the isolated desktop")
    user32.ShowWindow(hwnd, 6)
    settle(lambda: user32.IsIconic(hwnd))
    user32.ShowWindow(hwnd, 9)
    settle(lambda: not user32.IsIconic(hwnd))
    checks.append("Native Bit_pack window minimizes and restores")
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    assert process.wait(timeout=10) == 0
    checks.append("Native Bit_pack GUI closes cleanly")
    report = {"passed": True, "private_desktop": True, "checks": checks, "pid": process.pid}
except Exception:
    import traceback
    report = {"passed": False, "private_desktop": True, "checks": checks,
              "error": traceback.format_exc()}
finally:
    if process is not None and process.poll() is None:
        if hwnd:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)

raise SystemExit(write_report(report))