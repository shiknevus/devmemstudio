"""Run native GUI acceptance on a private desktop without visible test windows."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if os.name != "nt":
    print(json.dumps({"passed": True, "skipped": "native Bit_pack GUI requires Windows", "checks": []}, ensure_ascii=False))
    raise SystemExit(0)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
user32.CreateDesktopW.restype = w.HANDLE
user32.CloseDesktop.argtypes = [w.HANDLE]
user32.CloseDesktop.restype = w.BOOL

class StartupInfo(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
                ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
                ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD),
                ("wShowWindow", w.WORD), ("cbReserved2", w.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]

class ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]

kernel32.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                  w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR,
                                  ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)]
kernel32.CreateProcessW.restype = w.BOOL
kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
kernel32.WaitForSingleObject.restype = w.DWORD
kernel32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]

name = "DevmemStudio_BitPack_Acceptance_" + str(os.getpid())
desktop = user32.CreateDesktopW(name, None, None, 0, 0x10000000, None)
if not desktop:
    raise ctypes.WinError(ctypes.get_last_error())
process = ProcessInfo()
startup = StartupInfo()
startup.cb = ctypes.sizeof(startup)
startup.lpDesktop = name
startup.dwFlags = 1
startup.wShowWindow = 0
command = ctypes.create_unicode_buffer(subprocess.list2cmdline([
    sys.executable, "-X", "utf8", str(ROOT / "tools" / "verify_bitpack_gui_worker.py")]))
try:
    if not kernel32.CreateProcessW(sys.executable, command, None, None, False, 0x08000000,
                                  None, str(ROOT), ctypes.byref(startup), ctypes.byref(process)):
        raise ctypes.WinError(ctypes.get_last_error())
    if kernel32.WaitForSingleObject(process.hProcess, 60000) != 0:
        kernel32.TerminateProcess(process.hProcess, 1)
        raise TimeoutError("Private desktop GUI acceptance timed out")
    code = w.DWORD()
    kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(code))
    report = ROOT / "artifacts" / "bitpack-gui-acceptance" / "report.json"
    if not report.is_file():
        raise RuntimeError(f"GUI acceptance worker exited without report: {code.value}")
    result = json.loads(report.read_text(encoding="utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(code.value or (0 if result["passed"] else 1))
finally:
    if process.hThread:
        kernel32.CloseHandle(process.hThread)
    if process.hProcess:
        kernel32.CloseHandle(process.hProcess)
    user32.CloseDesktop(desktop)

