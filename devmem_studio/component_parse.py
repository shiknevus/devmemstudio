# -*- coding: utf-8 -*-
"""Shared RTL register parsing: used by the offline catalog generator and by the
EXE's 导入组件 feature (runtime re-parse of a single component folder)."""
from __future__ import annotations

from pathlib import Path
import re

from .top_import import read_text_resilient

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
    # connection expression itself. For concats keep every wire (with its bit select).
    for number, expression in PARAM_EXPR.findall(body):
        stripped = expression.strip()
        if stripped.startswith("{"):
            fields = extract_concat_fields(stripped)
            if fields:
                for item in fields:
                    add(number, item["name"])
                continue
        for name in IDENTIFIER.findall(SIZED_LITERAL.sub(" ", expression)):
            add(number, name)
    # Identifier-only port comments (.param51 (param51) // r_pf_abspos) carry the signal name.
    for name, note in notes.items():
        if IDENTIFIER.fullmatch(note):
            add(name[5:], note)
    return {"notes": notes, "behaviors": behaviors,
            "signals": {f"PARAM{number}": "/".join(names) for number, names in signals.items()}}


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


def element_width(element: str) -> int | None:
    """Width of one concat element: None for unknown plain identifiers."""
    if element.startswith("{") and element.endswith("}"):
        element = element[1:-1].strip()   # replication group {N{literal}}
    replication = REPLICATION.match(element)
    if replication:
        return int(replication.group(1)) * int(replication.group(2))
    sized = SIZED_LITERAL_LINE.match(element)
    if sized:
        return int(sized.group(1))
    selected = re.match(r"^[A-Za-z_]\w*\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]$", element)
    if selected:
        return abs(int(selected.group(1)) - int(selected.group(2))) + 1
    if re.match(r"^[A-Za-z_]\w*\s*\[\s*\d+\s*\]$", element):
        return 1
    if re.match(r"^[A-Za-z_]\w*$", element):
        return 1   # plain 1-bit wire in these hookups
    return None


def extract_concat_fields(expression: str) -> list[dict]:
    """Bit-field layout of a Verilog concat {a, b, c}: a takes the highest bits.

    Sized literals count toward the width (padding) but carry no name; plain
    identifiers are treated as 1-bit wires, indexed selections keep their range."""
    expression = expression.strip()
    if expression.startswith("{") and expression.endswith("}"):
        expression = expression[1:-1]
    elements = []
    total = 0
    for raw in split_top_level(expression):
        element = raw.strip()
        width = element_width(element)
        if width is None:
            return []
        total += width
        identifier = HOOKUP_IDENT.match(element)
        if identifier:
            elements.append({"name": identifier.group(0).replace(" ", ""),
                             "width": width})
        elif width:
            elements.append({"name": None, "width": width})  # sized constant padding
    fields, cursor = [], total
    for element in elements:
        low = cursor - element["width"]
        if element.get("name"):
            fields.append({"name": element["name"], "low": low,
                           "width": element["width"]})
        cursor = low
    return fields if fields else []


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


def build_type_entry(key: str, decode_path: Path, defines: dict[str, int],
                     rtl_root: Path | None = None) -> tuple[dict, list[str], list[str]]:
    """One catalog type entry from a decode file; returns (entry, forced_irq, warnings)."""
    warnings: list[str] = []
    registers = []
    for item in parse_decode_file(read_text_resilient(decode_path)):
        offset = defines.get(item["name"])
        if offset is None:
            warnings.append(f"{decode_path.name}: {item['name']} missing from reg_addr_pl.vh")
            continue
        entry = {"offset": f"0x{offset:03X}", "name": item["name"], "width": item["width"],
                 "readonly": not item["writable"]}
        if item["writable"] and not item["readable"]:
            entry["write_only"] = True
        registers.append(entry)
    # IRQ_REG1/2 are part of every component's shared header; never ship a table without them.
    present = {item["name"] for item in registers}
    forced = []
    for name, offset in (("IRQ_REG1", 0x000), ("IRQ_REG2", 0x004)):
        if name not in present:
            registers.append({"offset": f"0x{offset:03X}", "name": name, "width": 32, "readonly": True})
            forced.append(f"{key}:{name}")
    registers.sort(key=lambda item: int(item["offset"], 16))
    if rtl_root is not None:
        family = decode_path.relative_to(rtl_root).parts[-3] if len(decode_path.relative_to(rtl_root).parts) >= 3 else ""
        decode_file = decode_path.relative_to(rtl_root).as_posix()
    else:
        family = decode_path.parent.parent.name
        decode_file = decode_path.name
    entry = {"family": family, "decode_file": decode_file, "registers": registers}
    top_file = find_top_file(key, decode_path)
    if top_file is None:
        warnings.append(f"{key}: 未找到组件顶层 ec_*.sv，参数注释与行为表未收割")
        return entry, forced, warnings
    harvested = harvest_type_notes(top_file)
    if harvested["notes"]:
        entry["notes"] = harvested["notes"]
    if harvested["behaviors"]:
        entry["behaviors"] = harvested["behaviors"]
    signals = harvested.get("signals", {})
    top_body = strip_comments(read_text_resilient(top_file))
    # A hookup that survives comment stripping means the register is wired at all.
    param_hookups = {number: expression for number, expression in PARAM_EXPR.findall(top_body)}
    wired = set(param_hookups)
    debug_hookups = {number: expression for number, expression in DEBUG_HOOKUP.findall(top_body)}
    for item in registers:
        signal = signals.get(item["name"])
        if signal:
            item["signal"] = signal
        elif item["name"].startswith("PARAM") and item["name"][5:] not in wired:
            item["unwired"] = True
        # Concat hookups carry a per-bit layout: show every wire as a field.
        if item["name"].startswith("PARAM"):
            expression = param_hookups.get(item["name"][5:], "")
            if expression.lstrip().startswith("{"):
                fields = extract_concat_fields(expression)
                if fields:
                    item["fields"] = fields
        elif item["name"].startswith("DEBUG_REG"):
            expression = debug_hookups.get(item["name"][-1], "")
            if expression.lstrip().startswith("{"):
                fields = extract_concat_fields(expression)
                if fields:
                    item["fields"] = fields
    entry["debug_notes"] = debug_notes_for(key, registers, harvest_debug_snapshots(top_body))
    return entry, forced, warnings


CATALOG_SCHEMA = 7


def find_reg_addr_map(folder: Path) -> Path | None:
    """Walk up from a component folder to its include_files/reg_addr_pl.vh."""
    folder = Path(folder).resolve()
    for ancestor in [folder, *list(folder.parents)[:8]]:
        candidate = ancestor / "include_files" / "reg_addr_pl.vh"
        if candidate.is_file():
            return candidate
    return None


def parse_component_folder(folder: Path) -> tuple[dict, dict]:
    """Runtime 导入组件: re-parse one component folder into a catalog type entry."""
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"文件夹不存在：{folder}")
    decode_candidates = sorted([*folder.glob("ps_rw_pl_reg_*.sv"), *folder.glob("ps_rw_pl_reg_*.v")])
    if not decode_candidates:
        raise ValueError("文件夹中未找到 ps_rw_pl_reg_*.sv/.v 寄存器解码文件。")
    decode_path = decode_candidates[0]
    text = read_text_resilient(decode_path)
    if not any(marker in text for marker in PLAINTEXT_MARKERS):
        raise ValueError(f"解码文件不可识别（可能被加密或格式变化）：{decode_path.name}")
    defines_path = find_reg_addr_map(folder)
    if defines_path is None:
        raise ValueError("未在组件文件夹及其上级找到 include_files/reg_addr_pl.vh（寄存器地址表）。")
    defines = parse_define_table(read_text_resilient(defines_path))
    if not defines:
        raise ValueError(f"寄存器地址表为空或不可读：{defines_path}")
    entry, forced, warnings = build_type_entry(folder.name, decode_path, defines)
    info = {"decode_file": str(decode_path), "defines": str(defines_path),
            "top_file": "", "forced_irq": forced, "warnings": warnings,
            "imported_from": str(folder)}
    top_file = find_top_file(folder.name, decode_path)
    if top_file is not None:
        info["top_file"] = str(top_file)
    entry["imported_from"] = str(folder)
    return entry, info


def find_component_folders(root: Path) -> list[Path]:
    """Every folder under root that carries its own ps_rw_pl_reg module (batch 导入组件)."""
    root = Path(root)
    folders = {path.parent for path in root.rglob("ps_rw_pl_reg_*")
               if path.suffix.lower() in (".sv", ".v")}
    return sorted(folders)
