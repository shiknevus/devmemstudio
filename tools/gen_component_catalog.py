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
    ALIASES, CATALOG_SCHEMA, DEBUG_REG_NOTE, PLAINTEXT_MARKERS, build_type_entry,
    debug_notes_for, extract_concat_fields, find_component_folders, find_top_file,
    harvest_debug_snapshots, harvest_signal_widths, harvest_type_notes, parse_component_folder,
    parse_decode_file, parse_define_table, read_text_resilient, split_top_level,
    strip_comments, top_is_plaintext)
from devmem_studio.top_import import parse_top  # noqa: E402


def type_keys_for(tops: list[Path], report: dict) -> list[str]:
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
    define_maps = sorted(rtl_root.rglob("reg_addr_pl.vh"))   # sorted: deterministic pick
    if not define_maps:
        raise SystemExit("reg_addr_pl.vh not found under the RTL root")
    defines_path = define_maps[0]
    if len(define_maps) > 1:
        report["top_warnings"].append("多个 reg_addr_pl.vh，使用 " + str(defines_path) + "；其余："
                                      + ", ".join(str(path) for path in define_maps[1:]))
    defines = parse_define_table(read_text_resilient(defines_path))
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
        top_file = find_top_file(key, path)
        if top_file is not None and not top_is_plaintext(read_text_resilient(top_file)):
            report["unparsed_files"].append({"file": str(top_file), "reason": "not-plaintext-or-unrecognized-format"})
    active = {c["module_type"] for top in tops for c in parse_top(read_text_resilient(top))["components"]
              if not c["disabled"]} if tops else set()
    report["unmatched_top_types"] = sorted(t for t in active if t not in types)
    report["types"] = len(types)
    report["registers_total"] = sum(len(entry["registers"]) for entry in types.values())
    catalog = {"schema": CATALOG_SCHEMA, "generated_at": stamp,
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
