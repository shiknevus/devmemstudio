# -*- coding: utf-8 -*-
"""Run the test suite in parallel child processes.

    venv\\Scripts\\python.exe tools\\run_tests.py            # daily run
    venv\\Scripts\\python.exe tools\\run_tests.py --full     # also the slow release checks (build_exe.bat)
    venv\\Scripts\\python.exe tools\\run_tests.py test_ui test_monitor.DialogTests

Each class is cut into chunks of consecutive tests sized from the last run's timings
(build/test-times.json); chunks run longest first, one child process each.
"""
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"   # same ids as `unittest discover -s tests`
TIMES = ROOT / "build" / "test-times.json"
DEFAULT_COST = 0.5      # seconds, for a test with no recorded time
CHILD_OVERHEAD = 1.5    # interpreter + Qt import per chunk
CHILD_TIMEOUT = 900


def child_env(full):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    if full:
        env["DEVMEMSTUDIO_FULL_TESTS"] = "1"
    return env


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def collect(patterns):
    loader = unittest.TestLoader()
    if patterns:
        suite = loader.loadTestsFromNames([p.removeprefix("tests.") for p in patterns])
    else:
        suite = loader.discover(str(TESTS))
    tests = list(flatten(suite))
    broken = [t for t in tests if t.__class__.__module__ == "unittest.loader"]
    if broken or loader.errors:
        for test in broken:
            print(getattr(test, "_exception", test), file=sys.stderr)
        for error in loader.errors:
            print(error, file=sys.stderr)
        sys.exit("Test collection failed.")
    return [t.id() for t in tests]


def plan(ids, times, jobs):
    """Group by class, cut classes into chunks near total/(3*jobs) seconds, longest first."""
    classes = {}
    for test_id in ids:
        classes.setdefault(test_id.rsplit(".", 1)[0], []).append(test_id)
    total = sum(times.get(i, DEFAULT_COST) for i in ids)
    target = max(4.0, total / (jobs * 3))
    chunks = []
    for members in classes.values():
        chunk, cost = [], 0.0
        for test_id in members:
            seconds = times.get(test_id, DEFAULT_COST)
            if chunk and cost + seconds > target:
                chunks.append((cost, chunk))
                chunk, cost = [], 0.0
            chunk.append(test_id)
            cost += seconds
        chunks.append((cost, chunk))
    return sorted(chunks, key=lambda item: -item[0])


def run_chunk(ids, env, scratch):
    handle, list_path = tempfile.mkstemp(suffix=".json", dir=scratch)
    os.close(handle)
    result_path = list_path[:-5] + ".result.json"
    Path(list_path).write_text(json.dumps(ids), encoding="utf-8")
    start = time.perf_counter()
    try:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", list_path, result_path],
                              cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=CHILD_TIMEOUT)
        output, code = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        output, code = f"{exc.stdout or ''}{exc.stderr or ''}\nTIMEOUT after {CHILD_TIMEOUT} s", -1
    elapsed = time.perf_counter() - start
    try:
        result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = None
    return ids, result, code, output, elapsed


def child(list_path, result_path):
    """Run the listed tests in this process and write the outcome as JSON."""
    sys.path[:0] = [str(TESTS), str(ROOT)]
    ids = json.loads(Path(list_path).read_text(encoding="utf-8"))
    durations = {}

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            self._started = time.perf_counter()
            super().startTest(test)

        def stopTest(self, test):
            super().stopTest(test)
            durations[test.id()] = time.perf_counter() - self._started

    suite = unittest.defaultTestLoader.loadTestsFromNames(ids)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=0, resultclass=Result).run(suite)
    Path(result_path).write_text(json.dumps({
        "run": result.testsRun, "skipped": len(result.skipped), "durations": durations,
        "problems": [(kind, test.id(), text) for kind, items in (("FAIL", result.failures),
                                                                  ("ERROR", result.errors),
                                                                  ("UNEXPECTED SUCCESS", [(t, "") for t in result.unexpectedSuccesses]))
                     for test, text in items],
    }), encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description="Run DevmemStudio tests in parallel.")
    parser.add_argument("names", nargs="*", help="modules / classes / tests, e.g. test_ui or test_ui.UiTests")
    parser.add_argument("-j", "--jobs", type=int, default=max(2, min(8, (os.cpu_count() or 4) // 2)))
    parser.add_argument("--full", action="store_true", help="include slow release checks (DEVMEMSTUDIO_FULL_TESTS=1)")
    parser.add_argument("--child", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(*args.child)

    os.chdir(ROOT)
    sys.path[:0] = [str(TESTS), str(ROOT)]
    os.environ.update(child_env(args.full))   # collection imports the test modules
    ids = collect(args.names)
    try:
        times = json.loads(TIMES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        times = {}
    chunks = plan(ids, times, args.jobs)
    print(f"{len(ids)} tests in {len(chunks)} chunks, {args.jobs} jobs{' (full)' if args.full else ''}", flush=True)
    start = time.perf_counter()
    run = skipped = 0
    problems, crashed = [], []
    with tempfile.TemporaryDirectory(prefix="devmem-tests-") as scratch, \
            ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_chunk, chunk, child_env(args.full), scratch) for _, chunk in chunks]
        for done, future in enumerate(as_completed(futures), 1):
            chunk_ids, result, code, output, elapsed = future.result()
            label = chunk_ids[0].rsplit(".", 1)[0]
            if result is None:
                crashed.append((label, code, output))
                status = f"CRASHED (exit {code})"
            else:
                run += result["run"]
                skipped += result["skipped"]
                problems += result["problems"]
                times.update(result["durations"])
                status = "ok" if not result["problems"] else f"{len(result['problems'])} problem(s)"
            print(f"[{done:>2}/{len(chunks)}] {label} ({len(chunk_ids)}) {status} {elapsed:.1f}s", flush=True)
    TIMES.parent.mkdir(parents=True, exist_ok=True)
    TIMES.write_text(json.dumps(times, indent=0, sort_keys=True), encoding="utf-8")

    for kind, test_id, text in problems:
        print(f"\n{'=' * 70}\n{kind}: {test_id}\n{'-' * 70}\n{text}")
    for label, code, output in crashed:
        print(f"\n{'=' * 70}\nCRASHED: {label} (exit {code})\n{'-' * 70}\n{output[-4000:]}")
    elapsed = time.perf_counter() - start
    summary = f"Ran {run} tests in {elapsed:.1f}s"
    if skipped:
        summary += f", {skipped} skipped"
    if problems or crashed or run != len(ids):
        if run != len(ids) and not crashed:
            summary += f", expected {len(ids)}"
        print(f"\n{summary}\nFAILED ({len(problems)} problem(s), {len(crashed)} crashed chunk(s))")
        return 1
    print(f"\n{summary}\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
