# -*- coding: utf-8 -*-
"""Hand-over with the single-file launcher's splash (tools/runtime_launcher/launcher.c): start-up stages for its bar,
and "ready"/"gone" so the splash closes before the main window shows. No-ops without the launcher."""
import ctypes
from ctypes import wintypes
import os

QT, MODULES, UI, WINDOW, PROJECT = 5, 6, 7, 8, 9   # launcher.c STAGE_*
_STAGE_COUNT = 10
_NAME = os.environ.get("DEVMEMSTUDIO_LAUNCHER_SPLASH") if os.name == "nt" else None


class _Progress(ctypes.Structure):   # launcher.c Progress
    _fields_ = [("stage", ctypes.c_long), ("at", ctypes.c_longlong * _STAGE_COUNT)]


_progress = None


def _block():
    global _progress
    if _progress is None:
        _progress = False
        if _NAME:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.OpenFileMappingW.restype = wintypes.HANDLE
            kernel.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
            kernel.MapViewOfFile.restype = ctypes.c_void_p
            mapping = kernel.OpenFileMappingW(0x0006, False, _NAME + ".progress")   # FILE_MAP_READ | FILE_MAP_WRITE
            view = kernel.MapViewOfFile(mapping, 0x0006, 0, 0, ctypes.sizeof(_Progress)) if mapping else None
            if view:
                _progress = _Progress.from_address(view)   # mapped for the life of the process
    return _progress


def stage(number):
    """The start-up stage beginning now, for the launcher's bar."""
    block = _block()
    if block:
        now = ctypes.c_longlong()
        ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(now))
        block.at[number] = now.value
        block.stage = number


def _event(suffix, access):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    return kernel, kernel.OpenEventW(access, False, _NAME + suffix)


def show_main_window(window, timeout=0.5):
    """Say "ready", wait for "gone" (the splash fills its bar and closes), then show the window."""
    if _NAME:
        kernel, ready = _event(".ready", 0x0002)   # EVENT_MODIFY_STATE
        _, gone = _event(".gone", 0x00100000)   # SYNCHRONIZE
        try:
            if ready and gone and kernel.SetEvent(ready):
                kernel.WaitForSingleObject(gone, int(timeout * 1000))
        finally:
            for handle in (ready, gone):
                if handle:
                    kernel.CloseHandle(handle)
    window.show()
