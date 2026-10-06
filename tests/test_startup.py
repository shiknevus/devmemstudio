"""Start-up stays light (SSH stack on demand) and the single-file launch keeps a proper taskbar identity."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import unittest

from devmem_studio import taskbar

ROOT = Path(__file__).resolve().parents[1]


def run_python(code):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=180)


class StartupImportTests(unittest.TestCase):
    def test_window_import_defers_the_ssh_stack(self):
        result = run_python("import sys, devmem_studio.window\n"
                            "print(sorted(name for name in ('paramiko', 'cryptography', 'invoke') if name in sys.modules))")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_ssh_stack_works_without_invoke_and_openssl_hashlib(self):
        # Mirrors the release bundle, which excludes invoke, _hashlib and libcrypto-3.dll:
        # the real loopback SSH suite must still pass on built-in digests.
        result = run_python("import sys\n"
                            "sys.modules['invoke'] = None\n"
                            "sys.modules['_hashlib'] = None\n"
                            "import unittest\n"
                            "suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_ssh')\n"
                            "outcome = unittest.TextTestRunner(stream=sys.stdout, verbosity=0).run(suite)\n"
                            "from devmem_studio import core\n"
                            "import paramiko\n"
                            "print('BUNDLE-LIKE', outcome.wasSuccessful(), outcome.testsRun,"
                            " issubclass(core.board_transport(), paramiko.Transport))")
        self.assertEqual(result.returncode, 0, result.stderr)
        verdict = result.stdout.strip().splitlines()[-1].split()
        self.assertEqual(verdict[0], "BUNDLE-LIKE")
        self.assertEqual((verdict[1], verdict[3]), ("True", "True"), result.stdout + result.stderr)
        self.assertGreater(int(verdict[2]), 20)


class TaskbarIdentityTests(unittest.TestCase):
    def test_pinning_relaunches_the_single_exe(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        hwnd = user32.CreateWindowExW(0, "STATIC", "DevmemStudio probe", 0, 0, 0, 40, 40, None, None, None, None)
        self.assertTrue(hwnd, ctypes.get_last_error())
        try:
            launcher = r"D:\工具\DevmemStudio.exe"
            self.assertEqual(taskbar.read_relaunch(hwnd).get(taskbar.RELAUNCH_COMMAND, ""), "")
            self.assertTrue(taskbar.bind_relaunch(hwnd, launcher))
            values = taskbar.read_relaunch(hwnd)
            self.assertEqual(values[taskbar.APP_USER_MODEL_ID], taskbar.APP_ID)
            self.assertEqual(values[taskbar.RELAUNCH_COMMAND], f'"{launcher}"')
            self.assertEqual(values[taskbar.RELAUNCH_NAME], "DevmemStudio")
            self.assertEqual(values[taskbar.RELAUNCH_ICON], f"{launcher},0")
        finally:
            user32.DestroyWindow(hwnd)


if __name__ == "__main__":
    unittest.main()
