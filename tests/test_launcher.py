"""Single-file launcher around a tiny fake runtime, run on a private desktop: splash ownership, failure screen."""
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
IDCANCEL, IDRETRY, GW_OWNER, WM_COMMAND, WM_KEYDOWN, VK_ESCAPE = 2, 4, 4, 0x0111, 0x0100, 0x1B
SPLASH = "DevmemStudioLauncherSplash"
STAGES = range(5, 10)   # launcher.c STAGE_QT..STAGE_PROJECT

# The app the launcher runs: "--exit N" quits before any window, "--window" shows one, "--ready" first has the
# launcher take its splash down (as devmem_debug does) and quits with 10+ if any splash is still on screen;
# "--stages" reports the app's stages 60 ms apart before that.
FAKE_APP = r"""#include <windows.h>
#include <stdlib.h>
#include <string.h>
typedef struct { volatile LONG stage; volatile LONGLONG at[10]; } Progress;
int WINAPI WinMain(HINSTANCE a, HINSTANCE b, LPSTR c, int d)
{
    const char *code = strstr(c, "--exit ");
    if (code)
        return atoi(code + 7);
    if (strstr(c, "--ready")) {
        wchar_t name[96], event[112];
        if (!GetEnvironmentVariableW(L"DEVMEMSTUDIO_LAUNCHER_SPLASH", name, 96))
            return 10;
        if (strstr(c, "--late"))
            Sleep(800);   /* the splash is surely up and animating by now */
        if (strstr(c, "--stages")) {
            wsprintfW(event, L"%s.progress", name);
            HANDLE mapping = OpenFileMappingW(FILE_MAP_WRITE, FALSE, event);
            Progress *progress = mapping ? MapViewOfFile(mapping, FILE_MAP_WRITE, 0, 0, sizeof *progress) : NULL;
            if (!progress)
                return 14;
            for (int s = 5; s <= 9; ++s) {
                LARGE_INTEGER now;
                QueryPerformanceCounter(&now);
                progress->at[s] = now.QuadPart;
                progress->stage = s;
                Sleep(60);
            }
        }
        wsprintfW(event, L"%s.ready", name);
        HANDLE ready = OpenEventW(EVENT_MODIFY_STATE, FALSE, event);
        wsprintfW(event, L"%s.gone", name);
        HANDLE gone = OpenEventW(SYNCHRONIZE, FALSE, event);
        if (!ready || !gone || !SetEvent(ready))
            return 11;
        if (WaitForSingleObject(gone, 2000) != WAIT_OBJECT_0)
            return 12;
        for (HWND w = FindWindowW(L"DevmemStudioLauncherSplash", NULL); w; w = FindWindowExW(NULL, w, L"DevmemStudioLauncherSplash", NULL))
            if (IsWindowVisible(w))
                return 13;
    }
    if (strstr(c, "--window") || strstr(c, "--ready"))
        CreateWindowExA(0, "STATIC", "fake app", WS_POPUP | WS_VISIBLE, 0, 0, 40, 40, 0, 0, a, 0);
    Sleep(60000);
    return 0;
}
"""


class StartupInfo(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


if sys.platform == "win32":
    user32, kernel = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    EnumProc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user32.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
    user32.CreateDesktopW.restype = w.HANDLE
    user32.CloseDesktop.argtypes = [w.HANDLE]
    user32.EnumDesktopWindows.argtypes = [w.HANDLE, EnumProc, w.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user32.GetClassNameW.argtypes = user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [w.HWND]
    user32.GetWindow.argtypes = [w.HWND, w.UINT]
    user32.GetWindow.restype = w.HWND
    user32.GetDlgItem.argtypes = [w.HWND, ctypes.c_int]
    user32.GetDlgItem.restype = w.HWND
    user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    kernel.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
                                      ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInfo)]
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.c_void_p]


class ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]


def children(parent):
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)   # TH32CS_SNAPPROCESS
    entry, found = ProcessEntry(), []
    entry.dwSize = ctypes.sizeof(entry)
    more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
    while more:
        if entry.th32ParentProcessID == parent:
            found.append(entry.th32ProcessID)
        more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    kernel.CloseHandle(snapshot)
    return found


def read_timings(path):
    """launcher.c Timings: warm and cold ms per stage."""
    magic, version, *ms = struct.unpack("<4sI20d", path.read_bytes())
    return magic, version, ms[:10], ms[10:]


class Window:
    def __init__(self, handle):
        name, title = ctypes.create_unicode_buffer(128), ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(handle, name, 128)
        user32.GetWindowTextW(handle, title, 128)
        self.handle, self.name, self.title = handle, name.value, title.value
        self.visible = bool(user32.IsWindowVisible(handle))
        self.owned = bool(user32.GetWindow(handle, GW_OWNER))

    def button(self, ident):
        child = user32.GetDlgItem(self.handle, ident)
        return bool(child) and bool(user32.IsWindowVisible(child))


@unittest.skipUnless(sys.platform == "win32" and shutil.which("gcc"), "Windows and MinGW-w64 required")
class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="devmem-launcher-")
        cls.base = Path(cls.scratch.name)
        runtime = cls.base / "runtime"
        (runtime / "_internal").mkdir(parents=True)
        (runtime / "_internal" / "data.bin").write_bytes(os.urandom(4096))
        (cls.base / "app.c").write_text(FAKE_APP)
        subprocess.run([shutil.which("gcc"), str(cls.base / "app.c"), "-mwindows", "-o", str(runtime / "DevmemStudio.exe")],
                       check=True, capture_output=True, timeout=60)
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import build_singlefile
            build_singlefile.pack(runtime, cls.base / "DevmemStudio.exe", cls.base / "build")
        except SystemExit as error:   # no x86_64 MinGW on PATH
            cls.scratch.cleanup()
            raise unittest.SkipTest(str(error))
        finally:
            sys.path.remove(str(ROOT / "tools"))
        cls.exe = cls.base / "DevmemStudio.exe"

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def setUp(self):
        self.name = f"DevmemLauncherTest_{os.getpid()}_{id(self)}"
        self.desktop = user32.CreateDesktopW(self.name, None, None, 0, 0x10000000, None)   # GENERIC_ALL
        if not self.desktop:
            raise ctypes.WinError(ctypes.get_last_error())
        self.work = Path(tempfile.mkdtemp(prefix="run-", dir=self.base))
        self.process = ProcessInfo()

    def tearDown(self):
        self.stop()
        user32.CloseDesktop(self.desktop)
        deadline = time.monotonic() + 10
        while self.work.exists():
            try:
                shutil.rmtree(self.work)
            except PermissionError:   # the job ends the app a moment after the launcher
                if time.monotonic() > deadline:
                    raise AssertionError("the app outlived its launcher: the runtime is still in use") from None
                time.sleep(0.2)

    def stop(self):
        if self.process.hProcess:
            if kernel.WaitForSingleObject(self.process.hProcess, 0) != 0:
                kernel.TerminateProcess(self.process.hProcess, 1)   # its job takes the app along
                kernel.WaitForSingleObject(self.process.hProcess, 5000)
            kernel.CloseHandle(self.process.hThread)
            kernel.CloseHandle(self.process.hProcess)
        self.process = ProcessInfo()

    def start(self, arguments="", appdata=None):
        environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("DEVMEMSTUDIO_LAUNCHER")}
        environment["LOCALAPPDATA"] = str(appdata or self.work / "appdata")
        block = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(environment.items())) + "\0\0")
        startup = StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.lpDesktop = self.name
        command = ctypes.create_unicode_buffer(f'"{self.exe}" {arguments}')
        if not kernel.CreateProcessW(str(self.exe), command, None, None, False, 0x00000400,   # CREATE_UNICODE_ENVIRONMENT
                                     block, str(self.work), ctypes.byref(startup), ctypes.byref(self.process)):
            raise ctypes.WinError(ctypes.get_last_error())

    def windows(self):
        found = []

        @EnumProc
        def visit(handle, _):
            pid = w.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if pid.value == self.process.dwProcessId:
                found.append(Window(handle))
            return True

        user32.EnumDesktopWindows(self.desktop, visit, 0)
        return found

    def main_window(self):
        """What .NET's Process.MainWindowHandle (so apply_update.ps1) takes: unowned and visible. Only the
        launcher's own windows: without a taskbar, the input indicator adds an overlay once one has focus."""
        return next((x.handle for x in self.windows() if x.name in (SPLASH, "#32770") and x.visible and not x.owned), None)

    def wait_for(self, condition, seconds=15, what="condition"):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}; windows: {[(x.name, x.title, x.visible, x.owned) for x in self.windows()]}")

    def splash(self, failing):
        def find():
            return next((x for x in self.windows() if x.name == SPLASH and x.visible and x.button(IDRETRY) == failing), None)
        return self.wait_for(find, what="failure screen" if failing else "starting splash")

    def exit_code(self, seconds=10):
        self.assertEqual(kernel.WaitForSingleObject(self.process.hProcess, seconds * 1000), 0, "launcher still running")
        code = w.DWORD()
        kernel.GetExitCodeProcess(self.process.hProcess, ctypes.byref(code))
        return code.value

    def test_starting_splash_is_owned_so_updaters_do_not_see_an_error(self):
        self.start()   # unpacks, then the app waits without a window
        splash = self.splash(failing=False)
        self.assertTrue(splash.owned)
        self.assertEqual(splash.title, "")
        self.assertIsNone(self.main_window())
        self.assertTrue(any((self.work / "appdata" / "DevmemStudio" / "runtime").iterdir()))

    def test_splash_closes_once_the_app_shows_a_window(self):
        self.start("--window")
        self.wait_for(lambda: not any(x.name == SPLASH for x in self.windows()), what="splash to close")
        self.assertEqual(kernel.WaitForSingleObject(self.process.hProcess, 0), 0x102)   # WAIT_TIMEOUT: app still running

    def test_app_takes_the_splash_down_before_its_window(self):
        for attempt, arguments in enumerate(("--ready", "--ready --late")):   # while the splash comes up / once it is up
            self.start(arguments, appdata=self.work / f"appdata-{attempt}")
            child = self.wait_for(lambda: children(self.process.dwProcessId), what="the app process")[0]
            app = kernel.OpenProcess(0x00100000 | 0x1000, False, child)   # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            try:
                if kernel.WaitForSingleObject(app, 1500) == 0:
                    code = w.DWORD()
                    kernel.GetExitCodeProcess(app, ctypes.byref(code))
                    self.fail(f"attempt {attempt}: the app quit with {code.value} (12: no signal back, 13: splash still up)")
            finally:
                kernel.CloseHandle(app)
            self.assertFalse([x for x in self.windows() if x.name == SPLASH and x.visible])
            self.stop()

    def test_failure_shows_in_the_splash_and_retry_starts_again(self):
        appdata = self.work / "appdata"
        appdata.write_text("a file, not a directory")
        self.start(appdata=appdata)
        failure = self.splash(failing=True)
        self.assertFalse(failure.owned)
        self.assertTrue(failure.title.startswith("DevmemStudio "))
        self.assertTrue(failure.button(IDCANCEL))
        self.assertEqual(self.main_window(), failure.handle)   # an updater rolls back at once
        self.assertFalse([x for x in self.windows() if x.name == "#32770"])   # no error box besides
        appdata.unlink()
        appdata.mkdir()
        user32.PostMessageW(failure.handle, WM_COMMAND, IDRETRY, 0)
        splash = self.splash(failing=False)
        self.assertEqual(splash.handle, failure.handle)   # the same window carries on
        self.assertTrue(splash.owned)
        self.assertEqual(splash.title, "")
        self.wait_for(lambda: any((appdata / "DevmemStudio" / "runtime").glob("*")), what="the retried unpack")
        self.assertIsNone(self.main_window())

    def test_escape_cancels_the_failure(self):
        appdata = self.work / "appdata"
        appdata.write_text("a file, not a directory")
        self.start(appdata=appdata)
        failure = self.splash(failing=True)
        user32.PostMessageW(user32.GetDlgItem(failure.handle, IDRETRY), WM_KEYDOWN, VK_ESCAPE, 0)
        self.assertEqual(self.exit_code(), 3)

    def test_app_exit_before_any_window_is_a_failure(self):
        self.start("--exit 7")
        failure = self.splash(failing=True)
        self.assertFalse(failure.owned)
        user32.PostMessageW(failure.handle, WM_COMMAND, IDCANCEL, 0)
        self.assertEqual(self.exit_code(), 7)

    def test_killing_the_launcher_takes_the_app_along(self):
        """An updater kills a launcher while it starts; even inside CreateProcess, no suspended app may stay."""
        for attempt in range(5):
            self.start(appdata=self.work / f"appdata-{attempt}")
            child = self.wait_for(lambda: children(self.process.dwProcessId), seconds=15, what="the app process")[0]
            app = kernel.OpenProcess(0x00100000, False, child)   # SYNCHRONIZE
            self.stop()
            try:
                self.assertTrue(not app or kernel.WaitForSingleObject(app, 5000) == 0, f"attempt {attempt}: the app outlived its launcher")
            finally:
                if app:
                    kernel.CloseHandle(app)

    def test_clean_app_exit_without_window_is_not_a_failure(self):
        self.start("--exit 0")
        self.assertEqual(self.exit_code(), 0)

    def timings(self, appdata):
        try:
            return read_timings(next((Path(appdata) / "DevmemStudio" / "splash").glob("*.times")))
        except (StopIteration, OSError, struct.error):   # not yet, or being written
            return None

    def test_splash_shows_at_every_start(self):
        appdata = self.work / "appdata"
        for attempt in range(2):   # the second start unpacks nothing
            self.start("--ready --late", appdata=appdata)
            splash = self.splash(failing=False)
            self.assertTrue(splash.owned, f"attempt {attempt}")
            self.stop()

    def test_stage_times_are_learned_for_the_next_start(self):
        appdata = self.work / "appdata"

        def learned(kind, stage, default):   # kind 2: warm, 3: cold
            def check():
                found = self.timings(appdata)
                return found and found[kind][stage] != default and found[kind]
            return check

        self.start("--ready --stages", appdata=appdata)   # unpacks: a cold start, learned apart
        self.wait_for(learned(3, 2, 850), what="cold timings")
        self.stop()
        self.start("--ready --stages", appdata=appdata)
        warm = self.wait_for(learned(2, 5, 25), what="warm timings")
        for stage, default in zip(STAGES, (25, 55, 20, 490, 60)):   # each took 60 ms (or a timer tick more)
            self.assertLess(abs(warm[stage] - (default + 60) / 2), 15, (stage, warm))


if __name__ == "__main__":
    unittest.main()
