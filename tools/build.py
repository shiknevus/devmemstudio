# -*- coding: utf-8 -*-
"""Build, test and accept the release (called by build_exe.bat).

Tests and the source smoke run beside the packaging chain
(PyInstaller -> single-file EXE -> isolated EXE acceptance). The EXE is built and
accepted under build/ and only copied to dist/ once every check has passed.
Each step logs to build/logs/<step>.log.
"""
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "build" / "logs"
CANDIDATE = ROOT / "build" / "DevmemStudio.exe"   # accepted here before it reaches dist/
PY = sys.executable


def deps_current():
    """True when every pin in requirements-lock.txt is already installed at that version."""
    for line in (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines():
        pin = re.match(r"\s*([A-Za-z0-9_.\-]+)==([^\s;#]+)", line)
        if not pin:
            continue
        try:
            if version(pin.group(1)) != pin.group(2):
                return False
        except PackageNotFoundError:
            return False
    return True


def icon_current():
    icon = ROOT / "assets" / "devmem.ico"
    sources = (ROOT / "assets" / "logo.svg", ROOT / "tools" / "make_icon.py")
    return icon.is_file() and all(icon.stat().st_mtime >= source.stat().st_mtime for source in sources)


def run(name, *args, env=None):
    """Run one step with its output in build/logs/<name>.log; raise on failure."""
    log = LOGS / f"{name}.log"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {}))
    started = time.perf_counter()
    with log.open("w", encoding="utf-8", errors="replace") as handle:
        code = subprocess.run([PY, *map(str, args)], cwd=ROOT, env=env, stdout=handle,
                              stderr=subprocess.STDOUT).returncode
    seconds = time.perf_counter() - started
    print(f"  {'ok  ' if code == 0 else 'FAIL'} {name:<12} {seconds:6.1f} s", flush=True)
    if code:
        raise StepFailed(name, log)


class StepFailed(Exception):
    def __init__(self, name, log):
        super().__init__(name)
        self.name, self.log = name, log


def checks():
    run("tests", "tools/run_tests.py", "--full")


def smoke():
    run("smoke", "devmem_debug.py", "--offscreen", "--smoke-test", ROOT / "artifacts" / "build-smoke")


def package():
    # Byte-identical runtime for unchanged sources (fixed zip/PE stamps and base_library order),
    # so build_singlefile can reuse the previous LZMS payload.
    run("pyinstaller", "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", "build/dist", "DevmemStudio.spec",
        env={"SOURCE_DATE_EPOCH": "1767225600", "PYTHONHASHSEED": "0"})
    run("singlefile", "tools/build_singlefile.py", "--output", CANDIDATE)
    run("verify_exe", "tools/verify_exe.py", "--executable", CANDIDATE)


def main():
    no_dist = "--no-dist" in sys.argv[1:]   # build and accept build/DevmemStudio.exe only
    LOGS.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        if deps_current():
            print("  skip pip          (requirements-lock.txt already installed)")
        else:
            run("pip", "-m", "pip", "install", "-r", "requirements-lock.txt")
        if icon_current():
            print("  skip icon         (assets/devmem.ico is up to date)")
        else:
            run("icon", "tools/make_icon.py")
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(step) for step in (package, checks, smoke)]
            failures = [future.exception() for future in futures if future.exception()]
        if failures:
            raise failures[0]
        if no_dist:
            print(f"\nAccepted in {time.perf_counter() - started:.0f} s: {CANDIDATE} (dist/ untouched)")
            return 0
        target = ROOT / "dist" / "DevmemStudio.exe"
        target.parent.mkdir(exist_ok=True)
        try:
            shutil.copy2(CANDIDATE, target)
        except PermissionError:
            print(f"\n{target} is in use; close DevmemStudio, then run:\n"
                  f"  copy {CANDIDATE} {target}\n  venv\\Scripts\\python.exe tools\\make_release.py")
            return 1
        run("release", "tools/make_release.py")
    except StepFailed as failure:
        lines = failure.log.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"\n--- last lines of {failure.log} ---")
        print("\n".join(lines[-60:]))
        return 1
    print(f"\nBuild complete in {time.perf_counter() - started:.0f} s: {ROOT / 'dist' / 'DevmemStudio.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
