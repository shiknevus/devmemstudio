"""Run fast, dependency-free repository checks before the GUI regression suite."""
from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
import subprocess
import sys
import compileall


ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^\s*([A-Za-z0-9_.-]+)==([^\s;#]+)")


def locked_packages(path: Path) -> dict[str, str]:
    packages = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PIN.match(line)
        if match:
            packages[match.group(1).lower().replace("-", "_")] = match.group(2)
    return packages


def dependency_issues(path: Path) -> list[str]:
    issues = []
    for name, expected in locked_packages(path).items():
        try:
            actual = version(name)
        except PackageNotFoundError:
            issues.append(f"{name}: missing (expected {expected})")
            continue
        if actual != expected:
            issues.append(f"{name}: {actual} != {expected}")
    return issues


def catalog_issues(path: Path) -> list[str]:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"catalog unreadable: {exc}"]
    issues = []
    if not isinstance(catalog.get("types"), dict) or not catalog["types"]:
        issues.append("catalog has no types")
    source = catalog.get("source", {})
    if isinstance(source, dict) and Path(str(source.get("rtl_root", ""))).is_absolute():
        issues.append(f"catalog embeds absolute RTL path: {source['rtl_root']}")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-deps", action="store_true", help="skip installed dependency version checks")
    parser.add_argument("--strict-catalog", action="store_true",
                        help="run catalog generation in strict mode as well")
    args = parser.parse_args(argv)
    errors = []
    if not compileall.compile_dir(str(ROOT / "devmem_studio"), quiet=1):
        errors.append("devmem_studio compileall failed")
    if not compileall.compile_dir(str(ROOT / "tools"), quiet=1):
        errors.append("tools compileall failed")
    if not args.skip_deps:
        errors.extend(dependency_issues(ROOT / "requirements-lock.txt"))
    for warning in catalog_issues(ROOT / "devmem_studio/data/component_catalog.json"):
        print("WARNING:", warning)
    if args.strict_catalog:
        command = [sys.executable, str(ROOT / "tools/gen_component_catalog.py"), "--refresh", "--strict-catalog"]
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode:
            errors.append("strict catalog check failed")
    if errors:
        print("CHECK FAILED")
        print("\n".join(f"- {item}" for item in errors))
        return 1
    print("CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
