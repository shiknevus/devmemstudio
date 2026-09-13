# -*- coding: utf-8 -*-
"""Parse emcc mix-top files and map component types to generated register tables."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re

from .core import resource_path

REG_GRID_START = 0x800
REG_GRID_STEP = 0x200
CATALOG_RELPATH = "devmem_studio/data/component_catalog.json"

_HEADER = re.compile(r"^\s*//\s*(?:/\s*)*-{3,}\s*flow_comp_(\d+)\s*--(.*?)-{3,}\s*$", re.M)
_BIAS = re.compile(r"\.REG_SPACE_BIAS\s*\(\s*\d+'([dh])\s*([0-9a-fA-F_]+)")
_BASE_ADDR = re.compile(r"PL_CFG_BASE_ADDR[^\n]*?32'h([0-9a-fA-F]+(?:_[0-9a-fA-F]+)*)")
_IDENTIFIER = re.compile(r"^\s*(?://\s*)?([A-Za-z_]\w*)\s*$")

# Common header plus A/B/C channels; used when the type has no generated table.
FALLBACK_REGISTERS = [
    {"offset": "0x000", "name": "IRQ_REG1", "width": 32, "readonly": True},
    {"offset": "0x004", "name": "IRQ_REG2", "width": 32, "readonly": True},
    {"offset": "0x008", "name": "RST_EN", "width": 32},
    {"offset": "0x00C", "name": "EC_ID", "width": 32},
    {"offset": "0x010", "name": "SC_ID", "width": 32},
    {"offset": "0x014", "name": "BHV_PRIORITY", "width": 32},
    {"offset": "0x018", "name": "UNIT_ID", "width": 32},
    {"offset": "0x01C", "name": "UNIT_ECTRL", "width": 32},
    {"offset": "0x020", "name": "UNIT_ST", "width": 32},
    {"offset": "0x024", "name": "M_ID", "width": 32},
    {"offset": "0x028", "name": "M_ECTRL", "width": 32},
    {"offset": "0x02C", "name": "M_ST", "width": 32},
    {"offset": "0x030", "name": "M_WK_MOD", "width": 32},
    {"offset": "0x038", "name": "M_SAF_ST", "width": 32, "readonly": True},
    {"offset": "0x03C", "name": "LINK_M_SAF_ST", "width": 32, "readonly": True},
    {"offset": "0x054", "name": "A_TASK_ID", "width": 32},
    {"offset": "0x058", "name": "A_TASK_BHV_ID", "width": 32},
    {"offset": "0x05C", "name": "A_EN", "width": 32},
    {"offset": "0x060", "name": "EC_CHA_ST", "width": 32, "readonly": True},
    {"offset": "0x064", "name": "A_TX_OT", "width": 32},
    {"offset": "0x068", "name": "A_TX_RSULT_RPT", "width": 32, "readonly": True},
    {"offset": "0x06C", "name": "A_ALM_NUM", "width": 32, "readonly": True},
    {"offset": "0x070", "name": "A_TX_ID", "width": 32, "readonly": True},
    {"offset": "0x074", "name": "A_BHV_ID", "width": 32, "readonly": True},
    {"offset": "0x084", "name": "B_EN", "width": 32},
    {"offset": "0x088", "name": "EC_CHB_ST", "width": 32, "readonly": True},
    {"offset": "0x08C", "name": "B_TX_OT", "width": 32},
    {"offset": "0x090", "name": "B_TX_RSULT_RPT", "width": 32, "readonly": True},
    {"offset": "0x094", "name": "B_ALM_NUM", "width": 32, "readonly": True},
    {"offset": "0x098", "name": "B_TX_ID", "width": 32, "readonly": True},
    {"offset": "0x09C", "name": "B_BHV_ID", "width": 32, "readonly": True},
    {"offset": "0x0AC", "name": "C_EN", "width": 32},
    {"offset": "0x0B0", "name": "EC_CHC_ST", "width": 32, "readonly": True},
    {"offset": "0x0B4", "name": "C_TX_OT", "width": 32},
    {"offset": "0x0B8", "name": "C_TX_RSULT_RPT", "width": 32, "readonly": True},
    {"offset": "0x0BC", "name": "C_ALM_NUM", "width": 32, "readonly": True},
    {"offset": "0x0C0", "name": "C_TX_ID", "width": 32, "readonly": True},
    {"offset": "0x0C4", "name": "C_BHV_ID", "width": 32, "readonly": True},
    {"offset": "0x0C8", "name": "C_GAP_CRL", "width": 32},
]

# Register views over a component's exact table; PARAM registers are matched by prefix.
VIEW_ORDER = ("basic", "task_a", "task_b", "task_c", "irq", "param", "debug", "all")
VIEW_LABELS = {"all": "全部", "basic": "基础", "task_a": "A通道", "task_b": "B通道",
               "task_c": "C通道", "irq": "中断", "param": "参数", "debug": "调试"}


def _channel_set(channel: str) -> frozenset:
    registers = {f"{channel}_{part}" for part in ("EN", "TX_OT", "TX_ID", "ALM_NUM", "BHV_ID")}
    registers.add(f"EC_CH{channel}_ST")
    if channel == "A":
        registers |= {"A_TASK_ID", "A_TASK_BHV_ID"}
    if channel == "C":
        registers.add("C_GAP_CRL")
    return frozenset(registers)


VIEW_SETS = {
    "basic": frozenset({"RST_EN", "EC_ID", "SC_ID", "BHV_PRIORITY", "UNIT_ID", "UNIT_ECTRL",
                        "UNIT_ST", "M_ID", "M_ECTRL", "M_ST", "M_WK_MOD", "M_SAF_ST", "LINK_M_SAF_ST"}),
    "task_a": _channel_set("A"),
    "task_b": _channel_set("B"),
    "task_c": _channel_set("C"),
    # Report/ack registers belong to the interrupt handshake, not the channel trigger view.
    "irq": frozenset({"IRQ_REG1", "IRQ_REG2", "A_TX_RSULT_RPT", "B_TX_RSULT_RPT", "C_TX_RSULT_RPT"}),
    "debug": frozenset({"DEBUG_REG1", "DEBUG_REG2", "DEBUG_REG3"}),
}


def read_text_resilient(path: Path) -> str:
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")


def _decode_verilog_number(radix: str, digits: str) -> int:
    return int(digits.replace("_", ""), 16 if radix == "h" else 10)


def parse_top(text: str) -> dict:
    """Extract flow components from a mix-top source text."""
    components, warnings = [], []
    headers = list(_HEADER.finditer(text))
    for position, header in enumerate(headers):
        end = headers[position + 1].start() if position + 1 < len(headers) else min(header.end() + 200, len(text))
        block = text[header.end():end]
        seq = int(header.group(1))
        label = header.group(2).strip()
        module_type = instance = None
        disabled = False
        bias = None
        for line in block.splitlines():
            if bias is None:
                found = _BIAS.search(line)
                if found:
                    bias = _decode_verilog_number(found.group(1), found.group(2))
            if module_type is None or instance is None:
                candidate = _IDENTIFIER.match(line)
                if candidate:
                    commented = line.lstrip().startswith("//")
                    if module_type is None:
                        module_type, disabled = candidate.group(1), commented
                    else:
                        instance = candidate.group(1)
        if module_type is None or bias is None:
            warnings.append(f"flow_comp_{seq} 结构不完整，已跳过。")
            continue
        index = None
        if bias >= REG_GRID_START and (bias - REG_GRID_START) % REG_GRID_STEP == 0:
            index = (bias - REG_GRID_START) // REG_GRID_STEP
        else:
            warnings.append(f"flow_comp_{seq} 偏移 0x{bias:X} 不在 0x800+i*0x200 网格上。")
        code = label.split("_", 1)[0] if re.match(r"^[A-Z0-9]+_", label) else ""
        components.append({"seq": seq, "label": label, "code": code, "module_type": module_type,
                           "instance": instance or f"{module_type}_{seq}", "bias": bias,
                           "address": f"0x{bias:04x}", "index": index, "disabled": disabled,
                           "line": text.count("\n", 0, header.start()) + 1})
    return {"components": components, "warnings": warnings}


def find_components_param(top_path: Path) -> Path | None:
    """Walk up from the top file to locate include_files/components_param.vh."""
    for ancestor in [Path(top_path).resolve()] + list(Path(top_path).resolve().parents)[:6]:
        for candidate in (ancestor / "include_files/components_param.vh",):
            if candidate.is_file():
                return candidate
        for sibling in ancestor.glob("*/include_files/components_param.vh"):
            if sibling.is_file():
                return sibling
    return None


def parse_base_address(text: str) -> int | None:
    found = _BASE_ADDR.search(text)
    return int(found.group(1).replace("_", ""), 16) if found else None


def load_type_catalog(path: Path | None = None) -> dict | None:
    target = Path(path) if path else resource_path(CATALOG_RELPATH)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("types"), dict):
            return data
    except (OSError, ValueError):
        pass
    return None


def registers_for(catalog: dict | None, module_type: str) -> tuple[list[dict], bool]:
    """Register table for a component type; False when using the generic fallback."""
    entry = (catalog or {}).get("types", {}).get(module_type)
    if entry and isinstance(entry.get("registers"), list) and entry["registers"]:
        registers = sorted((dict(item) for item in entry["registers"]),
                           key=lambda item: int(str(item["offset"]), 16))
        # Every component implements the shared IRQ header; guarantee it even for odd tables.
        present = {item["name"] for item in registers}
        for name, offset in (("IRQ_REG1", "0x000"), ("IRQ_REG2", "0x004")):
            if name not in present:
                registers.insert(0 if name == "IRQ_REG1" else 1,
                                 {"offset": offset, "name": name, "width": 32, "readonly": True})
        return registers, True
    return copy.deepcopy(FALLBACK_REGISTERS), False


def reg_in_view(register: dict, view: str) -> bool:
    if view == "all":
        return True
    if view == "param":
        return str(register.get("name", "")).startswith("PARAM")
    return register.get("name") in VIEW_SETS.get(view, frozenset())


def view_registers(registers: list[dict], view: str) -> list[dict]:
    return [register for register in registers if reg_in_view(register, view)]


def type_metadata(catalog: dict | None, module_type: str) -> dict:
    """Harvested per-type semantics: register notes, behavior presets, debug notes."""
    entry = (catalog or {}).get("types", {}).get(module_type) or {}
    notes = entry.get("notes") if isinstance(entry.get("notes"), dict) else {}
    behaviors = entry.get("behaviors") if isinstance(entry.get("behaviors"), dict) else {}
    debug_notes = entry.get("debug_notes") if isinstance(entry.get("debug_notes"), dict) else {}
    return {"notes": {str(key): str(value) for key, value in notes.items()},
            "behaviors": {channel: [dict(item) for item in behaviors.get(channel, [])
                                    if isinstance(item, dict)] for channel in "ABC"},
            "debug_notes": {str(key): str(value) for key, value in debug_notes.items()}}
