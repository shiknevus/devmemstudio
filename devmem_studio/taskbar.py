# -*- coding: utf-8 -*-
"""Taskbar identity for the single-file build: pinning must restart the launcher, not the cached runtime."""
import ctypes
from ctypes import wintypes
import os
import uuid

APP_ID = "Sunny.DevmemStudio"
RELAUNCH_COMMAND, RELAUNCH_ICON, RELAUNCH_NAME, APP_USER_MODEL_ID = 2, 3, 4, 5
_APP_USER_MODEL = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
_IID_PROPERTY_STORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
_VT_LPWSTR = 31


def ensure_taskbar_window(hwnd):
    """Owned application windows minimize to the taskbar, without an iconic desktop frame."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_style, set_style = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style.restype = ctypes.c_ssize_t
    set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_style.restype = ctypes.c_ssize_t
    current = get_style(wintypes.HWND(hwnd), -20)   # GWL_EXSTYLE
    desired = (current | 0x00040000) & ~0x00000080   # WS_EX_APPWINDOW, not WS_EX_TOOLWINDOW
    if desired != current:
        ctypes.set_last_error(0)
        previous = set_style(wintypes.HWND(hwnd), -20, desired)
        if previous == 0 and ctypes.get_last_error():
            return False
    return True


def release_window_owner(hwnd):
    """Drop the Windows owner link created by the Qt parent.

    An owned window always stays above its owner. Once that window is maximized
    it covers the main window, and activating the main window cannot reveal it.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_long, set_long = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
    get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    get_long.restype = ctypes.c_ssize_t
    if not get_long(wintypes.HWND(hwnd), -8):   # GWLP_HWNDPARENT
        return   # already unowned: skip the frame-changing SetWindowPos (an extra repaint per state change)
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_long.restype = ctypes.c_ssize_t
    set_long(wintypes.HWND(hwnd), -8, 0)
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    # Apply the owner change without moving, sizing, or activating the window.
    user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)


def _with_taskbar(action):
    ole32 = ctypes.OleDLL("ole32")
    initialized = ctypes.windll.ole32.CoInitializeEx(None, 2) >= 0
    taskbar = ctypes.c_void_p()
    try:
        clsid = _Guid.parse("{56FDF344-FD6D-11d0-958A-006097C9A090}")
        iid = _Guid.parse("{56FDF342-FD6D-11d0-958A-006097C9A090}")
        # OleDLL raises OSError on a failing HRESULT, so a NULL taskbar below is
        # unreachable in production; the guard is defensive hardening only.
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(taskbar)):
            return False
        try:
            hr_init = _method(taskbar, 3)(taskbar)   # ITaskbarList::HrInit
            if hr_init not in (0, 1, -2147467263, 0x80004001):
                return False
            return action(taskbar)
        finally:
            if taskbar:   # never call Release on a null interface
                _method(taskbar, 2)(taskbar)
    except OSError:
        return False
    finally:
        if initialized:
            ctypes.windll.ole32.CoUninitialize()


def sync_taskbar_tabs(shown, hidden=()):
    """One tab per shown window; same-app tabs fold into one taskbar button.

    Hidden windows never get a tab: a tab added to a hidden window outlives it."""
    def apply(taskbar):
        add, remove = _method(taskbar, 4, wintypes.HWND), _method(taskbar, 5, wintypes.HWND)
        ok = True
        for hwnd in hidden:
            ok = remove(taskbar, wintypes.HWND(hwnd)) == 0 and ok
        for hwnd in shown:
            ok = add(taskbar, wintypes.HWND(hwnd)) == 0 and ok
        return ok
    return _with_taskbar(apply)


class _Guid(ctypes.Structure):
    _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD),
                ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        value = uuid.UUID(text)
        return cls(value.time_low, value.time_mid, value.time_hi_version, (ctypes.c_ubyte * 8)(*value.bytes[8:]))


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", wintypes.DWORD)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3), ("text", ctypes.c_wchar_p),
                ("padding", ctypes.c_void_p)]


def _method(store, index, *argtypes):
    table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(table[index])


def _with_store(hwnd, action):
    ole32, shell32 = ctypes.OleDLL("ole32"), ctypes.WinDLL("shell32")
    shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long   # HRESULT
    initialized = ctypes.windll.ole32.CoInitializeEx(None, 2) >= 0
    store = ctypes.c_void_p()
    try:
        if shell32.SHGetPropertyStoreForWindow(wintypes.HWND(hwnd), ctypes.byref(_Guid.parse(_IID_PROPERTY_STORE)),
                                               ctypes.byref(store)) != 0 or not store:
            return None
        try:
            return action(store, ole32)
        finally:
            if store:
                _method(store, 2)(store)   # IUnknown::Release
    finally:
        if initialized:
            ctypes.windll.ole32.CoUninitialize()


def _key(pid):
    return _PropertyKey(_Guid.parse(_APP_USER_MODEL), pid)


def bind_relaunch(hwnd, launcher):
    """Group the window under one app id whose pin / relaunch command is the single EXE."""
    def apply(store, _ole32):
        set_value = _method(store, 6, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))
        for pid, text in ((APP_USER_MODEL_ID, APP_ID), (RELAUNCH_COMMAND, f'"{launcher}"'),
                          (RELAUNCH_NAME, "DevmemStudio"), (RELAUNCH_ICON, f"{launcher},0")):
            if set_value(store, ctypes.byref(_key(pid)), ctypes.byref(_PropVariant(_VT_LPWSTR, (0, 0, 0), text))):
                return False
        return _method(store, 7)(store) == 0   # IPropertyStore::Commit
    return bool(_with_store(hwnd, apply))


def dismiss_launcher_splash(timeout=0.5):
    """Have the single-file launcher take its splash down (and wait) before the main window shows."""
    name = os.environ.get("DEVMEMSTUDIO_LAUNCHER_SPLASH")
    if not name:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    ready = kernel.OpenEventW(0x00100002, False, name + ".ready")   # SYNCHRONIZE | EVENT_MODIFY_STATE
    gone = kernel.OpenEventW(0x00100000, False, name + ".gone")   # SYNCHRONIZE
    try:
        return bool(ready and gone and kernel.SetEvent(ready)
                    and kernel.WaitForSingleObject(gone, int(timeout * 1000)) == 0)
    finally:
        for handle in (ready, gone):
            if handle:
                kernel.CloseHandle(handle)


def read_relaunch(hwnd):
    """The window's app id and relaunch properties (empty when unset)."""
    def read(store, ole32):
        get_value = _method(store, 5, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant))
        values = {}
        for pid in (APP_USER_MODEL_ID, RELAUNCH_COMMAND, RELAUNCH_NAME, RELAUNCH_ICON):
            value = _PropVariant()
            if get_value(store, ctypes.byref(_key(pid)), ctypes.byref(value)) == 0:
                values[pid] = value.text if value.vt == _VT_LPWSTR else ""
                ole32.PropVariantClear(ctypes.byref(value))
        return values
    return _with_store(hwnd, read) or {}
