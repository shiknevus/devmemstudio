# -*- coding: utf-8 -*-
"""Generate devmem_studio/data/component_catalog.json from the emcc RTL tree."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from devmem_studio.top_import import parse_top, read_text_resilient  # noqa: E402

ALIASES = {"ec_sf_door": "sf_doo"}
DEFINE = re.compile(r"^`define\s+(\w+)\s+9'h([0-9A-Fa-f]+)", re.M)
WRITE = re.compile(r"wr_task_addr\s*==\s*`(\w+)\s*\)\s*\?\s*i_st_wr_data(?:\s*\[(\d+)\s*:\s*(\d+)\])?")
READ_CASE = re.compile(r"case\s*\(\s*rd_addr_d2(?:\s*\[[^\]]*\])?\s*\)(.*?)endcase", re.S)
READ_PADDED = re.compile(r"`(\w+)\s*:[^\n]*?<=\s*\{(\d+)'d\d+\s*,")
PARAM_NOTE = re.compile(r"^[ \t]*,?[ \t]*\.[ \t]*([Pp][Aa][Rr][Aa][Mm]\d+)[ \t]*\([^()\n]*\)[ \t\r]*(?://[ \t]*(.*?))?[ \t\r]*$", re.M)
BHA_NUM = re.compile(r"localparam\s+([ABC])_BHA_NUM\s*=\s*\d+\s*;[ \t\r]*(?://[ \t]*(.*?))?[ \t\r]*$", re.M)
BEHAVIOR_PAIR = re.compile(r"(\d+)\s*([A-Za-z_][\w\-]*(?:\[[\w\-]+\])?)")
SIGNAL_PORT = re.compile(r"\.\s*([A-Za-z_]\w*)\s*\(\s*param(\d+)\s*(?:\[[^\]\n]*\])?\s*\)")
SIGNAL_ASSIGN = re.compile(r"assign\s+([A-Za-z_]\w*)\s*(?:\[[^\]\n]*\])?\s*=\s*param(\d+)\b(?:\[[^\]\n]*\])?")
PARAM_EXPR = re.compile(r"[,.]?\s*\.\s*param(\d+)\s*\(\s*([^()\n]*?)\s*\)", re.I)
SIZED_LITERAL = re.compile(r"\d+'\s*[sS]?[bdhBDH]\s*[0-9a-fA-F_xzXZ]+")
IDENTIFIER = re.compile(r"\b[A-Za-z_]\w*\b")
PLAINTEXT_MARKERS = ("wr_task_addr", "rd_addr_d2")

DEBUG_REG_NOTE = ("{channel} 通道行为状态机历史：每字节一个状态（前第3拍/前第2拍/前第1拍/当前）。"
                  "状态：0空闲 1预检 2发IRQ10 3等10应答 4发20 5执行中 7后检 8发30 9等30应答 "
                  "10发40 11等40应答 12/13结束")
DEBUG_HOOKUP = re.compile(r"[,.]?\s*\.\s*debug_reg(\d)\s*\(\s*([^()\n]*?)\s*\)", re.I)
REPLICATION = re.compile(r"^(\d+)\s*\{\s*(\d+)'\s*[sS]?[bdhBDH]\s*[0-9a-fA-F_xzXZ]+\s*\}$")
SIZED_LITERAL_LINE = re.compile(r"^(\d+)'\s*[sS]?[bdhBDH]\s*[0-9a-fA-F_xzXZ]+$")
HOOKUP_IDENT = re.compile(r"^([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?$")


def split_top_level(text: str) -> list[str]:
    parts, depth, current = [], 0, []
    for character in text:
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        if character == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return parts


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def parse_define_table(text: str) -> dict[str, int]:
    return {name: int(offset, 16) for name, offset in DEFINE.findall(text)}


def parse_decode_file(text: str) -> list[dict]:
    """Registers implemented by one ps_rw_pl_reg module: read case plus write ternaries."""
    body = strip_comments(text)
    writes = {name: (int(hi) - int(lo) + 1 if hi else None) for name, hi, lo in WRITE.findall(body)}
    case = READ_CASE.search(body)
    reads, widths = set(), {}
    if case:
        for name, pad in READ_PADDED.findall(case.group(1)):
            widths[name] = 32 - int(pad)
        reads = set(re.findall(r"`([A-Z]\w*)", case.group(1)))
    return [{"name": name, "writable": name in writes, "readable": name in reads,
             "width": writes.get(name) or widths.get(name) or 32}
            for name in sorted(reads | set(writes))]


def type_keys_for(tops: list[Path], report: dict) -> list[str]:
    keys = []
    for top in tops:
        info = parse_top(read_text_resilient(top))
        for component in info["components"]:
            if component["module_type"] not in keys:
                keys.append(component["module_type"])
        report["top_warnings"].extend(f"{top.name}: {message}" for message in info["warnings"])
    return keys


def find_top_file(key: str, decode_path: Path) -> Path | None:
    """The component top ec_<type>.sv/.v sits next to its ps_rw_pl_reg module."""
    wanted = key.replace("_", "").lower()
    candidates = sorted([*decode_path.parent.glob("ec_*.sv"), *decode_path.parent.glob("ec_*.v")])
    for candidate in candidates:
        if candidate.stem.startswith(("ps_rw_pl_reg_", "pre_post_", "proactive_", "status_", "tim_")):
            continue
        if candidate.stem.replace("_", "").lower() == wanted:
            return candidate
    return None


def harvest_type_notes(top_path: Path) -> dict:
    """Parameter comments, behavior tables and the real signal names behind PARAM registers."""
    text = read_text_resilient(top_path)
    notes = {name.upper(): note.strip() for name, note in PARAM_NOTE.findall(text) if note.strip()}
    behaviors = {}
    for channel, comment in BHA_NUM.findall(text):
        pairs = [{"name": name, "value": int(value)}
                 for value, name in BEHAVIOR_PAIR.findall(comment or "")]
        if pairs:
            behaviors[channel] = pairs
    signals = {}
    body = strip_comments(text)

    def add(number, name):
        if not name or re.fullmatch(r"(?i)param\d+", name):
            return  # self hookup or a cross-param reference, not a real signal name
        names = signals.setdefault(number, [])
        if name not in names:
            names.append(name)

    for port, number in SIGNAL_PORT.findall(body) + SIGNAL_ASSIGN.findall(body):
        add(number, port)
    # Reg-file hookups like .param64 ({7'd0,i_axis_limf}): the real signals sit in the
    # connection expression itself, next to sized literals and cross-param references.
    for number, expression in PARAM_EXPR.findall(body):
        for name in IDENTIFIER.findall(SIZED_LITERAL.sub(" ", expression)):
            add(number, name)
    # Identifier-only port comments (.param51 (param51) // r_pf_abspos) carry the signal name.
    for name, note in notes.items():
        if IDENTIFIER.fullmatch(note):
            add(name[5:], note)
    return {"notes": notes, "behaviors": behaviors,
            "signals": {f"PARAM{number}": "/".join(names) for number, names in signals.items()}}


def harvest_debug_snapshots(body: str) -> dict:
    """Registers wired as a concat of several signals are bit snapshots, not state history."""
    notes = {}
    for number, expression in DEBUG_HOOKUP.findall(body):
        expression = expression.strip()
        if expression.startswith("{") and expression.endswith("}"):
            expression = expression[1:-1]
        names, padding = [], 0
        for element in split_top_level(expression):
            element = element.strip()
            replication = REPLICATION.match(element)
            sized = SIZED_LITERAL_LINE.match(element)
            identifier = HOOKUP_IDENT.match(element)
            if element.startswith("{") and element.endswith("}"):
                replication = REPLICATION.match(element[1:-1])
            if replication:
                padding += int(replication.group(1)) * int(replication.group(2))
            elif sized:
                padding += int(sized.group(1))
            elif identifier and identifier.group(1).lower() != f"debug_reg{number}":
                names.append(identifier.group(1))
        if len(names) >= 2:
            legend = "\n".join(reversed(names))
            notes[f"DEBUG_REG{number}"] = ("位快照（低位→高位）：\n" + legend +
                                           (f"\n其余 {padding} 位为常数" if padding else ""))
    return notes


def debug_notes_for(key: str, registers: list[dict], snapshots: dict | None = None) -> dict:
    present = {item["name"] for item in registers}
    notes = {}
    for register, channel in (("DEBUG_REG1", "A"), ("DEBUG_REG2", "B"), ("DEBUG_REG3", "C")):
        if register in present:
            notes[register] = (snapshots or {}).get(register) or DEBUG_REG_NOTE.format(channel=channel)
    return notes


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
        registers = []
        for item in parse_decode_file(text):
            offset = defines.get(item["name"])
            if offset is None:
                report["top_warnings"].append(f"{path.name}: {item['name']} missing from reg_addr_pl.vh")
                continue
            entry = {"offset": f"0x{offset:03X}", "name": item["name"], "width": item["width"],
                     "readonly": not item["writable"]}
            if item["writable"] and not item["readable"]:
                entry["write_only"] = True
            registers.append(entry)
        # IRQ_REG1/2 are part of every component's shared header; never ship a table without them.
        present = {item["name"] for item in registers}
        for name, offset in (("IRQ_REG1", 0x000), ("IRQ_REG2", 0x004)):
            if name not in present:
                registers.append({"offset": f"0x{offset:03X}", "name": name, "width": 32, "readonly": True})
                report["forced_irq_registers"].append(f"{key}:{name}")
        registers.sort(key=lambda item: int(item["offset"], 16))
        entry = {"family": path.relative_to(rtl_root).parts[-3] if len(path.relative_to(rtl_root).parts) >= 3 else "",
                 "decode_file": path.relative_to(rtl_root).as_posix(),
                 "registers": registers}
        top_file = find_top_file(key, path)
        if top_file is None:
            report["unharvested_top_types"].append(key)
        else:
            harvested = harvest_type_notes(top_file)
            if harvested["notes"]:
                entry["notes"] = harvested["notes"]
            if harvested["behaviors"]:
                entry["behaviors"] = harvested["behaviors"]
            signals = harvested.get("signals", {})
            # A hookup that survives comment stripping means the register is wired at all.
            wired = {number for number, _ in PARAM_EXPR.findall(strip_comments(read_text_resilient(top_file)))}
            for item in registers:
                signal = signals.get(item["name"])
                if signal:
                    item["signal"] = signal
                elif item["name"].startswith("PARAM") and item["name"][5:] not in wired:
                    item["unwired"] = True
            snapshots = harvest_debug_snapshots(strip_comments(read_text_resilient(top_file)))
            entry["debug_notes"] = debug_notes_for(key, registers, snapshots)
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
