"""Preview and remove generated workspace artifacts without touching source data.

The default action is a dry run.  Use ``--apply`` only after reviewing the
candidate list.  The tool never removes ``venv`` or settings files; it only
handles old release archives, generated acceptance artifacts, and stale logs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
import shutil
import time
from pathlib import Path


RELEASE = re.compile(r"^DevmemStudio-(\d+(?:\.\d+){2})-win64\.zip(?:\.sha256)?$")
DEFAULT_KEEP_ARTIFACTS = frozenset({"build-smoke", "exe-isolated", "singlefile-build.json"})
DEFAULT_LOG_NAMES = frozenset({"build_bat.log", "build_serial.log"})


@dataclass(frozen=True)
class Candidate:
    path: Path
    size: int
    reason: str


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def latest_mtime(path: Path) -> float:
    """Return the newest file timestamp below a path, including the path itself."""
    newest = path.stat().st_mtime
    if path.is_dir():
        for child in path.rglob("*"):
            try:
                newest = max(newest, child.stat().st_mtime)
            except OSError:
                continue
    return newest


def directory_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def release_candidates(dist: Path, keep: int) -> list[Candidate]:
    grouped: dict[str, list[Path]] = {}
    if not dist.is_dir():
        return []
    for item in dist.iterdir():
        match = RELEASE.fullmatch(item.name)
        if match:
            grouped.setdefault(match.group(1), []).append(item)
    retained = {version for version in sorted(grouped, key=version_key, reverse=True)[:max(0, keep)]}
    return [Candidate(item, directory_size(item), f"旧发布版本 {version}")
            for version, items in grouped.items() if version not in retained for item in items]


def artifact_candidates(artifacts: Path, cutoff: float, keep_names: set[str]) -> list[Candidate]:
    if not artifacts.is_dir():
        return []
    result = []
    for item in artifacts.iterdir():
        if item.name in keep_names:
            continue
        try:
            if latest_mtime(item) >= cutoff:
                continue
        except OSError:
            continue
        result.append(Candidate(item, directory_size(item), "过期验收产物"))
    return result


def log_candidates(root: Path, cutoff: float) -> list[Candidate]:
    result = []
    for name in DEFAULT_LOG_NAMES:
        item = root / name
        if not item.is_file():
            continue
        try:
            if item.stat().st_mtime < cutoff:
                result.append(Candidate(item, directory_size(item), "过期根目录构建日志"))
        except OSError:
            continue
    return result


def build_plan(root: Path, *, days: int = 14, release_keep: int = 2,
               keep_artifacts: set[str] | None = None, now: float | None = None) -> list[Candidate]:
    """Build a safe cleanup plan.  No filesystem mutation occurs here."""
    if days < 0:
        raise ValueError("days must be non-negative")
    root = root.resolve()
    cutoff = (time.time() if now is None else now) - days * 86400
    keep = set(DEFAULT_KEEP_ARTIFACTS if keep_artifacts is None else keep_artifacts)
    plan = release_candidates(root / "dist", release_keep)
    plan.extend(artifact_candidates(root / "artifacts", cutoff, keep))
    plan.extend(log_candidates(root, cutoff))
    return sorted(plan, key=lambda item: str(item.path).lower())


def remove_candidates(root: Path, candidates: list[Candidate]) -> int:
    """Remove only paths already produced by ``build_plan`` under ``root``."""
    root = root.resolve()
    total = 0
    for candidate in candidates:
        path = candidate.path.resolve()
        if path == root or root not in path.parents:
            raise ValueError(f"refusing to remove path outside workspace: {path}")
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        total += candidate.size
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--days", type=int, default=14, help="retain artifacts newer than this many days")
    parser.add_argument("--release-keep", type=int, default=2, help="number of newest release versions to retain")
    parser.add_argument("--keep-artifact", action="append", default=[],
                        help="artifact name to retain; may be repeated")
    parser.add_argument("--apply", action="store_true", help="actually remove the listed paths")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"workspace does not exist: {root}")
    try:
        plan = build_plan(root, days=args.days, release_keep=args.release_keep,
                          keep_artifacts=set(args.keep_artifact) | set(DEFAULT_KEEP_ARTIFACTS))
    except ValueError as exc:
        parser.error(str(exc))
    total = sum(item.size for item in plan)
    action = "将删除" if args.apply else "预览（不会删除）"
    print(f"{action} {len(plan)} 项，{total / 1024 / 1024:.2f} MiB")
    for item in plan:
        print(f"{item.size / 1024 / 1024:8.2f} MiB  {item.reason:<12} {item.path}")
    if args.apply and plan:
        removed = remove_candidates(root, plan)
        print(f"已删除 {removed / 1024 / 1024:.2f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
