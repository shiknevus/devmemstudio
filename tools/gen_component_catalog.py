# -*- coding: utf-8 -*-
"""Generate devmem_studio/data/component_catalog.json from the emcc RTL tree."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from devmem_studio.component_parse import (  # noqa: E402,F401
    ALIASES, DEBUG_REG_NOTE, PLAINTEXT_MARKERS, build_type_entry, debug_notes_for,
    find_component_folders, find_top_file, harvest_debug_snapshots, harvest_type_notes,
    parse_component_folder, parse_decode_file, parse_define_table, read_text_resilient,
    split_top_level, strip_comments)
from devmem_studio.top_import import parse_top  # noqa: E402


def type_keys_for(tops: list[Path], report: dict) -> list[str]:
    keys = []
    for top in tops:
        info = parse_top(read_text_resilient(top))
        for component in info["components"]:
            if component["module_type"] not in keys:
                keys.append(component["module_type"])
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
            candidates.sort(key=lambda stem: (-len(stem), stem))
        if not candidates:
            candidates = [stem for stem in decode_files if suffix in stem]
        if not candidates:
            continue
        matched[key] = decode_files[candidates[0]]
    return matched, ambiguous


def build(rtl_root: Path, tops: list[Path], stamp: str) -> tuple[dict, dict]:
    report = {"top_warnings": [], "unparsed_files": [], "unmatched_top_types": [],
              "unharvested_top_types": [], "ambiguous": [], "forced_irq_registers": []}
    defines_path = next(rtl_root.rglob("reg_addr_pl.vh"), None)
    if defines_path is None:
        raise SystemExit("reg_addr_pl.vh not found under the RTL root")
    defines = parse_define_table(read_text_resilient(defines_path))
    if not defines:
        raise SystemExit(f"reg_addr map is empty or unreadable: {defines_path}")

    decode_files = {path.stem[len("ps_rw_pl_reg_"):]: path
                    for path in sorted(rtl_root.rglob("ps_rw_pl_reg_*"))}
    keys = type_keys_for(tops, report) if tops else []
    for stem in decode_files:
        directory = decode_files[stem].parent.name
        if directory not in keys:
            keys.append(directory)
    matched, ambiguous = match_decode_files(keys, decode_files)
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
        entry, forced, warnings = build_type_entry(key, path, defines, rtl_root)
        report["forced_irq_registers"].extend(forced)
        report["top_warnings"].extend(warnings)
        if "debug_notes" not in entry:
            report["unharvested_top_types"].append(key)
        types[key] = entry
    active = {c["module_type"] for top in tops for c in parse_top(read_text_resilient(top))["components"]
              if not c["disabled"]} if tops else set()
    report["unmatched_top_types"] = sorted(t for t in active if t not in types)
    report["types"] = len(types)
    report["registers_total"] = sum(len(entry["registers"]) for entry in types.values())
    catalog = {"schema": 6, "generated_at": stamp,
               "source": {"rtl_root": str(rtl_root), "reg_addr_map": defines_path.name,
                          "note": "由 tools/gen_component_catalog.py 生成；RTL 变更后需重新生成"},
               "types": types}
    return catalog, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rtl", required=True, type=Path, help="RTL root containing reg_addr_pl.vh and decode modules")
    parser.add_argument("--out", type=Path, default=ROOT / "devmem_studio/data/component_catalog.json")
    parser.add_argument("--tops", default="", help="comma-separated top files used to collect type keys")
    parser.add_argument("--stamp", default=None, help="override generated_at for reproducible output")
    parser.add_argument("--check", action="store_true", help="exit non-zero on unparsed files or unmatched top types")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    tops = [Path(item) for item in args.tops.split(",") if item.strip()]
    catalog, report = build(args.rtl.resolve(), tops, args.stamp or datetime.now().isoformat(timespec="seconds"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.verbose else None))
    if args.check and (report["unparsed_files"] or report["unmatched_top_types"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
