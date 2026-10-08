# -*- coding: utf-8 -*-
"""Parse emcc mix-top files and map component types to generated register tables."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re

from .core import parse_int, resource_path, user_data_dir

REG_GRID_START = 0x800
REG_GRID_STEP = 0x200
CATALOG_RELPATH = "devmem_studio/data/component_catalog.json"

_HEADER = re.compile(r"^[ \t]*//[ \t]*(?:/[ \t]*)*-{3,}[ \t]*flow_comp_(\d+)(?:[ \t]+\-+)*[ \t]*(.*?)[ \t]*\-*[ \t\r]*$", re.M)
# 20'h3000 / 20'H3000 / 'h3000 / 20'sd25600 / plain 25600
_BIAS = re.compile(r"\.REG_SPACE_BIAS\b\s*\(\s*(?:\d*\s*'\s*[sS]?([dDhH])\s*([0-9a-fA-F_]+)|(\d[\d_]*))\s*\)")
_BASE_ADDR = re.compile(r"^[ \t]*`define[ \t]+PL_CFG_BASE_ADDR[ \t]+"
                        r"(?:\{\s*32\s*'\s*[hH]\s*([0-9a-fA-F_]+)\s*\}|"
                        r"32\s*'\s*[hH]\s*([0-9a-fA-F_]+))[ \t\r]*$", re.M)
_IDENTIFIER = re.compile(r"^\s*(?://\s*)*([A-Za-z_]\w*)\s*$")

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


def find_top_files(root: Path, limit_bytes: int = 32 * 1024 * 1024) -> tuple[list[dict], bool]:
    """Mix-top candidates under a folder: .sv/.v files that carry ec_ controls.

    [{path, mtime, components}] ordered by control count, then newest; the
    second value is False when the folder walk hit a depth/size limit."""
    from .file_scan import find_files
    entries, complete = find_files(root, (".sv", ".v"), limit=20000)
    candidates = []
    for path, mtime, size in entries:
        if size > limit_bytes:
            continue
        try:
            text = read_text_resilient(path)
            if "ec_" not in text:
                continue
            components = parse_top(text)["components"]
        except Exception:
            continue   # unreadable or not Verilog: simply not a candidate
        active = sum(1 for component in components if not component["disabled"])
        if active:
            candidates.append({"path": path, "mtime": mtime, "components": active})
    candidates.sort(key=lambda item: (-item["components"], -item["mtime"]))
    return candidates, complete


def read_text_resilient(path: Path) -> str:
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")   # a BOM would hide the first ^-anchored `define / header
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")


def _decode_verilog_number(radix: str, digits: str) -> int:
    return int(digits.replace("_", ""), 16 if radix.lower() == "h" else 10)


def _bias_value(found) -> int:
    if found.group(3) is not None:
        return int(found.group(3).replace("_", ""))
    return _decode_verilog_number(found.group(1), found.group(2))


def _block_comments_as_line_comments(text: str) -> str:
    """Rewrite /* ... */ as // lines (same line count), so the comment-depth
    logic treats a block-commented instance as disabled, not active."""
    def convert(match):
        comment = match.group(0)
        if comment.startswith("//"):
            return comment
        if "\n" not in comment:
            return " " * len(comment)   # inline /* note */: code around it stays live
        return "\n".join("//" + line for line in comment[2:-2].split("\n"))
    return re.sub(r"//[^\n]*|/\*.*?\*/", convert, text, flags=re.S)


def _warn_duplicate_bias(components: list[dict], warnings: list[str]):
    seen = {}
    for component in components:
        if component["disabled"]:
            continue
        other = seen.setdefault(component["bias"], component)
        if other is not component:
            warnings.append(f"{other['instance']} 与 {component['instance']} 偏移同为 0x{component['bias']:X}，寄存器空间重叠。")


def _comment_depth(line: str) -> int:
    """Comment nesting depth: 0 = code, 1 = // line, 2 = // inside //, etc."""
    depth = 0
    rest = line.lstrip()
    while rest.startswith("//"):
        depth += 1
        rest = rest[2:].lstrip()
    return depth


def parse_top(text: str) -> dict:
    """Extract flow components from a mix-top source text.

    Every control is an ``ec_*`` instantiation. ``flow_comp_N`` headers, when
    present, supply the label, code and commented/disabled state. A block may
    carry a nested comment reference template plus the real ``ec_*`` instantiation
    below it; the component is the ``ec_*`` declaration at the shallowest comment
    depth (0 = active code, >0 = commented reference), so stale reference names
    never shadow the real module type.

    Active ``ec_*`` instantiations that sit outside those blocks — or that a
    header block did not already record — are still components. A file that
    mixes headed blocks with loose instances therefore yields both. Sources
    with no ``flow_comp_N`` header are parsed from the ``ec_*`` instantiations
    alone.
    """
    components, warnings = [], []
    text = _block_comments_as_line_comments(text)
    headers = list(_HEADER.finditer(text))
    if not headers:
        return _parse_unheaded(text)
    for position, header in enumerate(headers):
        if position + 1 < len(headers):
            end = headers[position + 1].start()
        else:
            tail = text.find("endmodule", header.end())
            end = tail if tail != -1 else len(text)
        block = text[header.end():end]
        seq = int(header.group(1))
        label = header.group(2).strip()
        nonempty = [line for line in block.splitlines() if line.strip()]
        by_depth: dict[int, list] = {}
        for line in nonempty:
            by_depth.setdefault(_comment_depth(line), []).append(line)
        source = None
        disabled = False
        for depth in sorted(by_depth):
            identifiers = [match.group(1) for line in by_depth[depth]
                           if (match := _IDENTIFIER.match(line)) and match.group(1).startswith("ec_")]
            if identifiers:
                source = by_depth[depth]
                disabled = depth > 0
                break
        if source is None:
            source = nonempty  # block without any ec_* declaration: report as-is
            disabled = 0 not in by_depth   # nothing but comments: not an active component
        module_type = instance = None
        bias = None
        invalid_bias = False
        # Remove only the selected comment layer, then trailing notes. Disabled
        # components stay discoverable, but stale literals in notes do not count.
        source = [re.sub(r"^\s*(?://\s*)*", "", line).split("//", 1)[0] for line in source]
        for line in source:
            if bias is None and not invalid_bias:
                found = _BIAS.search(line)
                if found:
                    try:
                        bias = _bias_value(found)
                    except ValueError:
                        invalid_bias = True
                elif re.search(r"\.REG_SPACE_BIAS\b", line):
                    invalid_bias = True
            if module_type is None or instance is None:
                candidate = _IDENTIFIER.match(line)
                if candidate and candidate.group(1).startswith("ec_"):
                    if module_type is None:
                        module_type = candidate.group(1)
                    else:
                        instance = candidate.group(1)
        if module_type is None:
            # No ec_* name anywhere (e.g. only a bias line): fall back to any identifier.
            for line in source:
                if module_type is None:
                    candidate = _IDENTIFIER.match(line)
                    if candidate:
                        module_type = candidate.group(1)
                elif instance is None:
                    candidate = _IDENTIFIER.match(line)
                    if candidate:
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
        instance = instance or f"{module_type}_{seq}"
        if not label:
            label = instance  # no header label: show the instance name instead of a blank row
        components.append({"seq": seq, "label": label, "code": code, "module_type": module_type,
                           "instance": instance, "bias": bias,
                           "address": f"0x{bias:04x}", "index": index, "disabled": disabled,
                           "line": text.count("\n", 0, header.start()) + 1})
    # Header blocks are not the whole file: an ec_* instantiation before the
    # first header, between blocks, or written in a shape the line scanner
    # missed is still a control. Skip names the header pass already recorded
    # so a block and its loose match are not listed twice.
    known = frozenset(item["instance"] for item in components)
    loose = _parse_unheaded(text, skip_instances=known, finalize=False)
    if loose["components"]:
        components.extend(loose["components"])
        components.sort(key=lambda item: (item["line"], item["instance"]))
        warnings.extend(message for message in loose["warnings"] if "寄存器空间重叠" not in message)
    _warn_duplicate_bias(components, warnings)
    return {"components": components, "warnings": warnings}


_EC_INSTANCE_HEAD = re.compile(r"\b(ec_\w+)\s*#\s*\(")
"""Loose ``ec_*`` instantiation head ``ec_type #(``; the params run to the
matching ``)`` (found by paren counting, so one instance never bleeds into the
next) and the instance name follows it: ``ec_type #( ... ) ec_type_27 (``."""


def _instance_seq(instance: str) -> int:
    """Trailing number from an instance name (``ec_x_27`` -> 27), else -1."""
    match = re.search(r"_(\d+)$", instance)
    return int(match.group(1)) if match else -1


def _parse_unheaded(text: str, skip_instances: frozenset[str] | None = None, finalize: bool = True) -> dict:
    """Active ``ec_*`` instantiations, used alone or to fill gaps around headers.

    Matches each active ``ec_*`` module instantiation in the code and takes its
    ``REG_SPACE_BIAS`` as the register-space bias (grid address semantics, the
    same 0x800 + i*0x200 convention the headed path uses). ``skip_instances``
    drops names a header block already recorded. ``finalize`` adds the
    overlapping-bias warning; the headed path recomputes that on the merged list.
    """
    skip_instances = skip_instances or frozenset()
    components, warnings = [], []
    # Blank // comments (same length) so commented instances, biases and stray
    # parens in comments never count; offsets stay valid for line numbers.
    code = re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), text)
    for match in _EC_INSTANCE_HEAD.finditer(code):
        module_type = match.group(1)
        depth, cursor = 1, match.end()
        while cursor < len(code) and depth:
            depth += {"(": 1, ")": -1}.get(code[cursor], 0)
            cursor += 1
        if depth:
            warnings.append(f"{module_type} 参数列表括号不配对，已跳过。")
            continue
        params = code[match.end():cursor - 1]
        named = re.match(r"\s*([A-Za-z_]\w*)\s*\(", code[cursor:])
        if not named:
            warnings.append(f"{module_type} 实例名未识别，已跳过。")
            continue
        instance = named.group(1)
        if not instance.startswith("ec_") or instance in skip_instances:
            continue
        bias = None
        found = _BIAS.search(params)
        if found:
            try:
                bias = _bias_value(found)
            except ValueError:
                pass
        if bias is None:
            warnings.append(f"{instance} 缺少或无法解析 .REG_SPACE_BIAS 常量，已跳过。")
            continue
        index = None
        if bias >= REG_GRID_START and (bias - REG_GRID_START) % REG_GRID_STEP == 0:
            index = (bias - REG_GRID_START) // REG_GRID_STEP
        else:
            warnings.append(f"{instance} 偏移 0x{bias:X} 不在 0x800+i*0x200 网格上。")
        seq = _instance_seq(instance)
        label = instance
        line = text.count("\n", 0, match.start()) + 1
        components.append({"seq": seq if seq >= 0 else len(components) + 1,
                           "label": label, "code": "", "module_type": module_type,
                           "instance": instance, "bias": bias,
                           "address": f"0x{bias:04x}", "index": index,
                           "disabled": False, "line": line})
    if finalize:
        _warn_duplicate_bias(components, warnings)
    return {"components": components, "warnings": warnings}


def find_components_param(top_path: Path) -> Path | None:
    """Walk up from the top file to locate include_files/components_param.vh."""
    for ancestor in [Path(top_path).resolve()] + list(Path(top_path).resolve().parents)[:6]:
        for candidate in (ancestor / "include_files/components_param.vh",):
            if candidate.is_file():
                return candidate
        try:   # one stat per child, not a directory listing per child like glob("*/…")
            children = list(os.scandir(ancestor))
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            sibling = Path(child.path) / "include_files/components_param.vh"
            if sibling.is_file():
                return sibling
    return None


def parse_base_address(text: str) -> int | None:
    # Match a complete active macro, never an old address in a comment or the
    # prefix of an expression. Unknown expressions require manual confirmation.
    code = re.sub(r"//[^\n]*|/\*.*?\*/",
                  lambda match: re.sub(r"[^\n]", " ", match.group(0)), text, flags=re.S)
    found = _BASE_ADDR.search(code)
    if found:
        try:
            return int((found.group(1) or found.group(2)).replace("_", ""), 16)
        except ValueError:
            pass
    return None


def load_type_catalog(path: Path | None = None) -> dict | None:
    """Bundled catalog plus any per-type overrides imported at runtime (导入组件)."""
    target = Path(path) if path else resource_path(CATALOG_RELPATH)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not (isinstance(data, dict) and isinstance(data.get("types"), dict)):
            return None
    except (OSError, ValueError):
        return None
    data["types"] = {key: normalized for key, entry in data["types"].items()
                     if (normalized := _normalize_catalog_entry(entry)) is not None}
    # Tests and offline acceptance opt out of the machine's real overrides via env flag.
    if path is None and not os.environ.get("DEVMEMSTUDIO_IGNORE_OVERRIDES"):
        overrides = user_data_dir() / "component_overrides"
        if overrides.is_dir():
            for item in sorted(overrides.glob("*.json")):
                try:
                    entry = json.loads(item.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                normalized = _normalize_catalog_entry(entry)
                if normalized is not None:
                    data["types"][item.stem] = normalized
    return data


def _parse_offset(item) -> int | None:
    """Register offset from a catalog/override entry; None when malformed."""
    try:
        offset = int(str(item["offset"]).strip(), 16)
        return offset if 0 <= offset <= 0xFFFFFFFF else None
    except (KeyError, TypeError, ValueError):
        return None


def _valid_named_values(items) -> list[dict]:
    """Preset/behavior entries need a display name and a numeric write value."""
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name, value = item.get("name"), parse_int(item.get("value"))
        if isinstance(name, str) and name.strip() and value is not None and 0 <= value < (1 << 64):
            result.append(copy.deepcopy(item))
    return result


def _valid_fields(items) -> list[dict]:
    """Keep only bit layouts within the 32-bit decoded register word."""
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        low, width = parse_int(item.get("low")), parse_int(item.get("width"))
        if (isinstance(name, str) and name.strip() and low is not None and width is not None
                and 0 <= low < 32 and 1 <= width <= 32 - low):
            result.append(dict(item, low=low, width=width))
    return result


def _normalize_catalog_entry(entry) -> dict | None:
    """Validate external register tables before they reach GUI/command code."""
    if not isinstance(entry, dict) or not isinstance(entry.get("registers"), list):
        return None
    registers = []
    for item in entry["registers"]:
        if not isinstance(item, dict) or _parse_offset(item) is None:
            continue
        name, width = item.get("name"), parse_int(item.get("width", 32))
        if not isinstance(name, str) or not name.strip() or width is None or not 1 <= width <= 64:
            continue
        register = copy.deepcopy(item)
        register["width"] = width
        for key in ("aliases", "buttons"):
            if key in register:
                register[key] = _valid_named_values(register[key])
        if "fields" in register:
            register["fields"] = _valid_fields(register["fields"])
        registers.append(register)
    if not registers:
        return None
    normalized = copy.deepcopy(entry)
    normalized["registers"] = registers
    return normalized


def _type_entry(catalog, module_type) -> dict:
    types = catalog.get("types") if isinstance(catalog, dict) else None
    entry = types.get(module_type) if isinstance(types, dict) else None
    return entry if isinstance(entry, dict) else {}


def registers_for(catalog: dict | None, module_type: str) -> tuple[list[dict], bool]:
    """Register table for a component type; False when using the generic fallback."""
    entry = _normalize_catalog_entry(_type_entry(catalog, module_type))
    if entry:
        registers = sorted(entry["registers"], key=_parse_offset)
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
    entry = _type_entry(catalog, module_type)
    notes = entry.get("notes") if isinstance(entry.get("notes"), dict) else {}
    behaviors = entry.get("behaviors") if isinstance(entry.get("behaviors"), dict) else {}
    debug_notes = entry.get("debug_notes") if isinstance(entry.get("debug_notes"), dict) else {}
    return {"notes": {str(key): str(value) for key, value in notes.items()},
            "behaviors": {channel: _valid_named_values(behaviors.get(channel)) for channel in "ABC"},
            "debug_notes": {str(key): str(value) for key, value in debug_notes.items()}}
