# -*- coding: utf-8 -*-
"""Generate devmem_studio/data/component_catalog.json from the emcc RTL tree."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
PORTABLE_RTL_ENV = "${DEVMEMSTUDIO_RTL}"
sys.path.insert(0, str(ROOT))
from devmem_studio.component_parse import (  # noqa: E402,F401
    ALIASES, CATALOG_SCHEMA, DEBUG_REG_NOTE, PLAINTEXT_MARKERS, build_type_entry,
    debug_notes_for, extract_concat_fields, find_component_folders, find_top_file, folder_module_types,
    harvest_debug_snapshots, harvest_signal_widths, harvest_type_notes, parse_component_folder,
    parse_decode_file, parse_define_table, read_text_resilient, split_top_level,
    strip_block_comments, strip_comments, top_is_plaintext)
from devmem_studio.core import component_override_dirs  # noqa: E402
from devmem_studio.top_import import (  # noqa: E402
    _catalog_timestamp, find_top_files, override_imported_at, parse_top)


def portable_rtl_root(path: Path) -> str:
    """Encode an RTL root without putting a developer machine path in the catalog."""
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(ROOT).as_posix() or "."
    except ValueError:
        return PORTABLE_RTL_ENV


def resolve_recorded_rtl(value: str | os.PathLike | None) -> Path | None:
    """Resolve a catalog source path; external trees use DEVMEMSTUDIO_RTL."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw in (PORTABLE_RTL_ENV, "$DEVMEMSTUDIO_RTL", "@env:DEVMEMSTUDIO_RTL"):
        raw = os.environ.get("DEVMEMSTUDIO_RTL", "").strip()
        if not raw:
            return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def type_keys_for(tops: list[Path], report: dict, active: set | None = None) -> list[str]:
    """Module types instantiated by the tops; `active` collects the enabled ones."""
    keys = []
    for top in tops:
        text = read_text_resilient(top)
        info = parse_top(text)
        if not info["components"] and not top_is_plaintext(text):
            report["unparsed_files"].append({"file": str(top), "reason": "not-plaintext-or-unrecognized-format"})
            continue
        for component in info["components"]:
            if component["module_type"] not in keys:
                keys.append(component["module_type"])
            if active is not None and not component["disabled"]:
                active.add(component["module_type"])
        report["top_warnings"].extend(f"{top.name}: {message}" for message in info["warnings"])
    return keys


def match_decode_files(keys: list[str], decode_files: dict[str, Path]) -> tuple[dict[str, Path], list[str]]:
    """Map each type key to a decode file: underscore-insensitive exact, then prefix."""
    def normalized(text: str) -> str:
        return text.replace("_", "").lower()

    matched, ambiguous = {}, []
    for key in keys:
        suffix = ALIASES.get(key, key[3:] if key.startswith("ec_") else key)
        candidates = [stem for stem in decode_files if normalized(stem) == normalized(suffix)]
        if not candidates:
            candidates = [stem for stem in decode_files
                          if stem.startswith(suffix) or suffix.startswith(stem)]
        if len(candidates) > 1:
            ambiguous.append(f"{key} -> {sorted(candidates)}")
            # Closest name wins: smallest length gap to the key, then the shorter stem.
            candidates.sort(key=lambda stem: (abs(len(normalized(stem)) - len(normalized(suffix))), len(stem), stem))
        if not candidates:
            candidates = [stem for stem in decode_files if suffix in stem]
        if not candidates:
            continue
        matched[key] = decode_files[candidates[0]]
    return matched, ambiguous


def build(rtl_root: Path, tops: list[Path], stamp: str) -> tuple[dict, dict]:
    report = {"top_warnings": [], "unparsed_files": [], "unmatched_top_types": [],
              "unharvested_top_types": [], "ambiguous": [], "forced_irq_registers": []}
    define_maps = sorted(rtl_root.rglob("reg_addr_pl.vh"))   # sorted: deterministic pick
    if not define_maps:
        raise SystemExit("reg_addr_pl.vh not found under the RTL root")
    defines_path = define_maps[0]
    if len(define_maps) > 1:
        report["top_warnings"].append("多个 reg_addr_pl.vh，使用 " + str(defines_path) + "；其余："
                                      + ", ".join(str(path) for path in define_maps[1:]))
    defines = parse_define_table(read_text_resilient(defines_path), report["top_warnings"])
    if not defines:
        raise SystemExit(f"reg_addr map is empty or unreadable: {defines_path}")

    decode_files = {}
    for path in sorted(rtl_root.rglob("ps_rw_pl_reg_*")):
        if not path.is_file() or path.suffix.lower() not in (".sv", ".v"):
            continue   # skip .bak copies and directories
        stem = path.stem[len("ps_rw_pl_reg_"):]
        if stem in decode_files:
            report["top_warnings"].append(f"解码文件重名 {stem}：{decode_files[stem]} 与 {path}，使用前者")
            continue
        decode_files[stem] = path
    active = set()   # enabled top types, for the coverage check below
    keys = type_keys_for(tops, report, active) if tops else []
    declared = {}   # module ec_* in the component folder -> that folder's decode file
    for stem in decode_files:
        directory = decode_files[stem].parent.name
        if directory not in keys:
            keys.append(directory)
        for name in folder_module_types(decode_files[stem].parent):
            if name in declared:
                report["top_warnings"].append(f"模块 {name} 在多个组件目录声明，使用 {declared[name]}")
                continue
            declared[name] = decode_files[stem]
    matched, ambiguous = match_decode_files([key for key in keys if key not in declared], decode_files)
    matched.update(declared)
    report["ambiguous"] = ambiguous

    types = {}
    for key in sorted(matched):
        path = matched[key]
        try:
            text = read_text_resilient(path)
        except OSError as exc:
            report["unparsed_files"].append({"file": str(path), "reason": str(exc)})
            continue
        if not any(marker in text for marker in PLAINTEXT_MARKERS):
            report["unparsed_files"].append({"file": str(path), "reason": "not-plaintext-or-unrecognized-format"})
            continue
        top_file = find_top_file(key, path)
        entry, forced, warnings = build_type_entry(key, path, defines, rtl_root, top_file=top_file)
        report["forced_irq_registers"].extend(forced)
        report["top_warnings"].extend(warnings)
        if "debug_notes" not in entry:
            report["unharvested_top_types"].append(key)
        types[key] = entry
        if top_file is not None and not top_is_plaintext(read_text_resilient(top_file)):
            report["unparsed_files"].append({"file": str(top_file), "reason": "not-plaintext-or-unrecognized-format"})
    report["unmatched_top_types"] = sorted(t for t in active if t not in types)
    report["types"] = len(types)
    report["registers_total"] = sum(len(entry["registers"]) for entry in types.values())
    catalog = {"schema": CATALOG_SCHEMA, "generated_at": stamp,
               "source": {"rtl_root": portable_rtl_root(rtl_root), "reg_addr_map": defines_path.name,
                          "note": "由 tools/gen_component_catalog.py 生成；RTL 变更后需重新生成"},
               "types": types}
    return catalog, report


def carry_removed(catalog: dict, previous: dict) -> list[str]:
    """Keep types the RTL no longer has (old bits still use them); same decode file -> fresh entry."""
    by_decode = {entry.get("decode_file"): entry for entry in catalog["types"].values()}
    kept = sorted(key for key in previous if key not in catalog["types"])
    for key in kept:
        catalog["types"][key] = json.loads(json.dumps(by_decode.get(previous[key].get("decode_file"), previous[key])))
    catalog["types"] = dict(sorted(catalog["types"].items()))
    return kept


def catalog_errors(report: dict, strict: bool = False) -> list[str]:
    """Return release-blocking catalog issues without changing the report format."""
    errors = []
    if report.get("unparsed_files"):
        errors.append("unparsed_files")
    if report.get("unmatched_top_types"):
        errors.append("unmatched_top_types")
    if strict and report.get("unharvested_top_types"):
        errors.append("unharvested_top_types")
    return errors


def _override_time(item: Path) -> float:
    try:
        entry = json.loads(item.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        entry = None   # unreadable: fall back to the mtime
    return override_imported_at(item, entry)


def local_override_dirs() -> list[Path]:
    """「更新控件」 folders on the build machine, including the one beside the built dist EXE."""
    return component_override_dirs() + [ROOT / "dist" / "component_overrides"]


def refresh(out: Path, rtl: Path | None, stamp: str, strict: bool = False) -> int:
    """Build step: regenerate from the recorded RTL root; rewrite only when something changed."""
    try:
        previous = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    source = previous.get("source", {})
    recorded = str(rtl) if rtl is not None else str(
        os.environ.get("DEVMEMSTUDIO_RTL") or (source.get("rtl_root", "") if isinstance(source, dict) else ""))
    root = Path(rtl).resolve() if rtl is not None else resolve_recorded_rtl(recorded)
    # Test fixtures and portable source trees may place the catalog beside the
    # RTL tree.  This fallback keeps those catalogs refreshable without restoring
    # the old absolute path in the JSON; normal releases should set the env var.
    if root is None and recorded == PORTABLE_RTL_ENV:
        colocated = out.parent / "include_files" / "reg_addr_pl.vh"
        if colocated.is_file():
            root = out.parent.resolve()
    if root is None or not root.is_dir():
        if strict and recorded:
            print("FAIL: stale or unavailable RTL root; set DEVMEMSTUDIO_RTL or pass --rtl")
            return 1
        print(f"skip: RTL root not found ({recorded or '未记录'}); bundled catalog unchanged")
        return 0
    # Type keys come from each folder's module declaration; tops only add aliases and the coverage check.
    tops = sorted(Path(item["path"]) for item in find_top_files(root / "top")[0]) if (root / "top").is_dir() else []
    catalog, report = build(root, tops, stamp)
    print("tops:", ", ".join(str(top) for top in tops) or "(none)")
    print(json.dumps(report, ensure_ascii=False))
    errors = catalog_errors(report, strict)
    if errors:
        print("FAIL: " + ", ".join(errors) + " (see report above)")
        return 1
    old_types = previous.get("types", {})
    kept = carry_removed(catalog, old_types)
    bundled_at = _catalog_timestamp(previous.get("generated_at"))
    newer = sorted({item.stem for folder in local_override_dirs() if folder.is_dir()
                    for item in folder.glob("*.json")
                    if bundled_at is None or _override_time(item) >= bundled_at})
    changed = sorted(key for key in catalog["types"] if catalog["types"][key] != old_types.get(key))
    if not changed and not newer:
        print(f"up to date: {len(catalog['types'])} types, generated_at {previous.get('generated_at')}")
        return 0
    out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"updated: {len(catalog['types'])} types; changed {changed}; kept removed {kept}; "
          f"newer local imports {newer}")   # a newer stamp lets the bundled table win over them
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtl", type=Path, help="RTL root containing reg_addr_pl.vh and decode modules")
    parser.add_argument("--refresh", action="store_true",
                        help="build step: optional tops from <rtl>/top, keep removed types, skip when unchanged")
    parser.add_argument("--out", type=Path, default=ROOT / "devmem_studio/data/component_catalog.json")
    parser.add_argument("--tops", default="", help="optional comma-separated top files: extra type aliases + coverage check")
    parser.add_argument("--stamp", default=None, help="override generated_at for reproducible output")
    parser.add_argument("--check", action="store_true", help="exit non-zero on unparsed files or unmatched top types")
    parser.add_argument("--strict-catalog", action="store_true",
                        help="also fail when a type lacks harvested top-level notes/behavior metadata")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    stamp = args.stamp or datetime.now().astimezone().isoformat(timespec="seconds")   # with UTC offset
    if args.refresh:
        return refresh(args.out, args.rtl, stamp, strict=args.strict_catalog)
    if args.rtl is None:
        parser.error("--rtl is required unless --refresh is given")
    tops =[Path(item) for item in args.tops.split(",") if item.strip()]
    catalog, report = build(args.rtl.resolve(), tops, stamp)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.verbose else None))
    if args.check and catalog_errors(report, args.strict_catalog):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
