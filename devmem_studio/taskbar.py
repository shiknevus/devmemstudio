# -*- coding: utf-8 -*-
"""Taskbar identity for the single-file build: pinning must restart the launcher, not the cached runtime."""
import ctypes
from ctypes import wintypes
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
    set_long = user32.SetWindowLongPtrW
    set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_long.restype = ctypes.c_ssize_t
    set_long(wintypes.HWND(hwnd), -8, 0)   # GWLP_HWNDPARENT
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    # Apply the owner change without moving, sizing, or activating the window.
    user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)


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
    initialized = ctypes.windll.ole32.CoInitializeEx(None, 2) >= 0
    store = ctypes.c_void_p()
    try:
        if shell32.SHGetPropertyStoreForWindow(wintypes.HWND(hwnd), ctypes.byref(_Guid.parse(_IID_PROPERTY_STORE)),
                                               ctypes.byref(store)) != 0 or not store:
            return None
        try:
            return action(store, ole32)
        finally:
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
