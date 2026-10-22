# -*- coding: utf-8 -*-
"""Shared RTL register parsing: used by the offline catalog generator and by the
EXE's 更新控件 feature (runtime re-parse of a single component folder)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from .top_import import COMMENT_SCAN, read_text_resilient

ALIASES = {"ec_sf_door": "sf_doo"}
# 9'h1B4 / 12'd16 / 12'b1010 / plain 436. A plain number must end the line; a
# sized literal may be followed only by a trailing comment — any other tail
# (``+ 9'h004``, ``- 20'h1000``, a macro reference) is an unresolved expression
# and is reported, never silently truncated to its prefix.
DEFINE = re.compile(r"^[ \t]*`define[ \t]+(\w+)[ \t]+(?:\d*[ \t]*'[ \t]*[sS]?([hHbBdDoO])[ \t]*([0-9a-fA-F_xXzZ_]+)"
                    r"(?:[ \t\r]*(?://[^\n]*)?$|$)|(\d[\d_]*)[ \t\r]*(?://[^\n]*)?$)", re.M)
_WR_ADDR = r"wr_task_addr(?:\s*\[[^\]]*\])?\s*==\s*`(\w+)\b"
# i_st_wr_data[15:0], optionally behind high padding: {16'd0, i_st_wr_data[15:0]}.
_WR_DATA = (r"(?:\{\s*(?:\d+\s*'\s*[sS]?[bdhBDH]\s*[0-9a-fA-F_xzXZ]+\s*,\s*)+)?"
            r"i_st_wr_data(?:\s*\[\s*(\d+)\s*(?::\s*(\d+)\s*)?\])?")
WRITE = re.compile(_WR_ADDR + r"[^?;]*?\?\s*" + _WR_DATA)
WRITE_IF = re.compile(r"\bif\s*\([^;]*?" + _WR_ADDR + r"[^;]*?\)\s*(?:begin\b\s*)?"
                      r"[A-Za-z_]\w*(?:\s*\[[^\]]*\])*\s*<?=\s*" + _WR_DATA)
# An assignment forwarding the write bus, possibly behind constant padding:
# ``target <= i_st_wr_data[7:0]`` / ``target <= {16'd0, i_st_wr_data[15:0]}``.
_WR_IF_ASSIGN = re.compile(r"\b([A-Za-z_]\w*(?:\s*\[[^\]]*\])*)\s*(?:<=|=)\s*"
                           r"(?:\{\s*(?:\d+\s*'\s*[sS]?[bdhBDH]\s*[0-9a-fA-F_xzXZ]+\s*,\s*)+)?"
                           r"i_st_wr_data(?:\s*\[\s*(\d+)\s*(?::\s*(\d+)\s*)?\])?")
READ_CASE = re.compile(r"case\s*\(\s*rd_addr_d2(?:\s*\[[^\]]*\])?\s*\)(.*?)endcase", re.S)
# Case item labels: a macro list (```A, `B :``) opening an arm, i.e. after the case
# head, a ``;``, ``begin`` or ``end``.  RHS macros (concat, ``? `X : y``, ``[`MSB:0]``)
# are data, not registers.
_READ_TAG = re.compile(r"(?:^|;|\bbegin\b|\bend\b)\s*((?:`\w+\s*,\s*)*`\w+)\s*:(?!:)")
# One read arm only ([^;\n] stops at the arm's end); <= or =, concat value.
READ_CONCAT = re.compile(r"`(\w+)\s*:[^;\n]*?<?=\s*(\{[^;\n]*\})\s*;")
# A read arm that slices the output bus: ```A : o_st_rd_data[7:0] <= …``.
_READ_SLICE = re.compile(r"`(\w+)\s*:[^;\n]*?o_st_rd_data\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*<?=")
_RD_ADDR = r"\b(?:rd_addr_d1|rd_addr_d2|rd_task_addr)\b(?:\s*\[[^\]]*\])?"
READ_COMPARE = re.compile(_RD_ADDR + r"\s*[!=]==?\s*`(\w+)|`(\w+)\s*[!=]==?\s*" + _RD_ADDR)
DECLARATION_KEYWORD = re.compile(r"\b(?:input|output|inout|wire|reg|logic)\b")
DECLARATION_HEAD = re.compile(r"\b(?:input|output|inout|wire|reg|logic)\b"
                              r"((?:\s+(?:wire|reg|logic|var|signed|unsigned)\b)*)\s*((?:\[[^\]]*\]\s*)*)")
DECLARATION_KEYWORDS = {"input", "output", "inout", "wire", "reg", "logic", "var", "signed", "unsigned"}
PARAM_NOTE = re.compile(r"^[ \t]*,?[ \t]*\.[ \t]*([Pp][Aa][Rr][Aa][Mm]\d+)[ \t]*\((?:[^()\n]|\([^()\n]*\))*\)"
                        r"[ \t]*,?[ \t\r]*(?://[ \t]*(.*?))?[ \t\r]*$", re.M)
BHA_NUM = re.compile(r"^[ \t]*localparam\s+([ABC])_BHA_NUM\s*=\s*\d+\s*;[ \t\r]*(?://[ \t]*(.*?))?[ \t\r]*$", re.M)
BEHAVIOR_PAIR = re.compile(r"(\d+)\s*([A-Za-z_][\w\-]*(?:\[[\w\-]+\])?)")
SIGNAL_PORT = re.compile(r"\.\s*([A-Za-z_]\w*)\s*\(\s*param(\d+)\s*(?:\[[^\]\n]*\])?\s*\)")
SIGNAL_ASSIGN = re.compile(r"assign\s+([A-Za-z_]\w*)\s*(?:\[[^\]\n]*\])?\s*=\s*param(\d+)\b(?:\[[^\]\n]*\])?")
PARAM_HEAD = re.compile(r"\.\s*param(\d+)\s*\(", re.I)
SIZED_LITERAL = re.compile(r"\d+'\s*[sS]?[bdhBDH]\s*[0-9a-fA-F_xzXZ]+")
IDENTIFIER = re.compile(r"\b[A-Za-z_]\w*\b")
PLAINTEXT_MARKERS = ("wr_task_addr", "rd_addr_d2")

DEBUG_REG_NOTE = ("{channel} 通道行为状态机历史：每字节一个状态（前第3拍/前第2拍/前第1拍/当前）。"
                  "状态：0空闲 1预检 2发IRQ10 3等10应答 4发20 5执行中 7后检 8发30 9等30应答 "
                  "10发40 11等40应答 12/13结束")
DEBUG_HEAD = re.compile(r"\.\s*debug_reg(\d)\s*\(", re.I)
REPLICATION = re.compile(r"^(\d+)\s*\{\s*(\d+)'\s*[sS]?[bdhBDH]\s*[0-9a-fA-F_xzXZ]+\s*\}$")
SIZED_LITERAL_LINE = re.compile(r"^(\d+)'\s*[sS]?[bdhBDH]\s*[0-9a-fA-F_xzXZ]+$")
HOOKUP_IDENT = re.compile(r"^([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?$")


def split_top_level(text: str) -> list[str]:
    """Split on commas outside {} and () groups."""
    parts, depth, current = [], 0, []
    for character in text:
        if character in "{(":
            depth += 1
        elif character in "})":
            depth -= 1
        if character == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return parts


def _scan_comments(text: str, keep_line_notes: bool) -> str:
    def replace(match):
        token = match.group(0)
        if token.startswith("/*"):
            return "\n" * token.count("\n")
        if token.startswith("//"):
            return token if keep_line_notes else ""
        return token   # string literal / escaped identifier
    return COMMENT_SCAN.sub(replace, text)


def strip_comments(text: str) -> str:
    """Drop // and /* */ comments in one left-to-right pass (whichever opens first wins).

    Verilog string literals (``"…"`` with ``\\"`` escapes, ending at the line) and
    escaped identifiers never open a comment, so real code inside such lines survives.
    Block comments keep their newlines so single-line patterns never merge lines."""
    return _scan_comments(text, keep_line_notes=False)


def strip_block_comments(text: str) -> str:
    """Drop /* */ comments only (same scan as strip_comments), keeping // trailing notes."""
    return _scan_comments(text, keep_line_notes=True)


def port_hookups(body: str, head: re.Pattern) -> list[tuple[str, str]]:
    """(number, expression) per `.portN ( expr )`; parens are counted, so nested and multi-line hookups work."""
    found = []
    for match in head.finditer(body):
        depth, cursor = 1, match.end()
        while cursor < len(body) and depth:
            depth += {"(": 1, ")": -1}.get(body[cursor], 0)
            cursor += 1
        if not depth:
            found.append((match.group(1), " ".join(body[match.end():cursor - 1].split())))
    return found


def parse_define_table(text: str, warnings: list[str] | None = None) -> dict[str, int]:
    """Register offsets from reg_addr_pl.vh; a later redefinition wins (warned when it differs).

    Sized literals may use h/d/b/o radix.  A ``define`` whose value is an
    expression (``9'h0D8 + 9'h004``, ``20'h3000 - 20'h1000``, a macro reference)
    is not a plain constant: it is skipped *and* reported, so a register is never
    silently placed at the truncated prefix of an expression.  Unsized literals
    are decimal (Verilog has no leading-zero octal: ``047`` is 47)."""
    table: dict[str, int] = {}
    values: dict[str, set[int]] = {}
    code = strip_comments(text)
    for name, radix, digits, plain in DEFINE.findall(code):
        try:
            if plain:
                value = int(plain.replace("_", ""))
            else:
                base = {"h": 16, "H": 16, "b": 2, "B": 2, "o": 8, "O": 8, "d": 10, "D": 10}[radix]
                # x/X (unknown) and z/Z (high impedance) are not base digits;
                # substitute 0 so a 'hz mask or 'bz defaults stays parseable.
                cleaned = digits.replace("_", "").replace("x", "0").replace("X", "0") \
                                  .replace("z", "0").replace("Z", "0")
                value = int(cleaned, base)
        except ValueError:
            if warnings is not None:
                warnings.append(f"reg_addr_pl.vh: {name} 的常量无法解析（{digits or plain}），已跳过。")
            continue
        if name in table and table[name] != value and warnings is not None:
            warnings.append(f"reg_addr_pl.vh: {name} 重复定义且取值不同（0x{table[name]:X} / 0x{value:X}），使用后者")
        table[name] = value
        values.setdefault(name, set()).add(value)
    # A `define whose value is not a plain constant (an expression or a macro
    # reference) is skipped and reported, never silently truncated to a prefix.
    # One regex covers both kinds and the names are de-duplicated so a line that
    # matches both (e.g. ```define X `BASE + 9'h100``) warns exactly once.
    nonconstant = re.compile(r"^[ \t]*`define[ \t]+(\w+)[ \t]+(?:`[A-Za-z_]\w*|[^ \t\r\n]+[ \t]*(?:\+|-|\*|/|<<|>>))", re.M)
    if warnings is not None:
        warned = set()
        for name in nonconstant.findall(code):
            if name not in warned:
                warned.add(name)
                warnings.append(f"reg_addr_pl.vh: {name} 的偏移是表达式或宏引用，无法解析，已跳过（请改为常量）。")
    # Conditional-compilation macros: every branch's ``define`` is collected
    # (later wins), but the active branch depends on compile-time flags this
    # parser cannot know, so a differing redefinition inside `ifdef must be
    # surfaced instead of silently picking the textually-last value.
    if warnings is not None and re.search(r"^\s*`(?:ifdef|ifndef|elsif|else|endif)\b", code, re.M):
        differing = sorted(name for name, seen in values.items() if len(seen) > 1)
        if differing:
            warnings.append("reg_addr_pl.vh: 含条件编译宏（`ifdef/`else/`endif），以下寄存器在多个分支中定义，"
                            "无法确定板端激活分支，请人工核对：" + "、".join(differing))
    return table


def _literal_width(element: str) -> int | None:
    """Width of a constant concat element (8'd0 or {24{1'b0}}); None for anything else."""
    element = element.strip()
    if element.startswith("{") and element.endswith("}"):
        element = element[1:-1].strip()
    replication = REPLICATION.match(element)
    if replication:
        return int(replication.group(1)) * int(replication.group(2))
    sized = SIZED_LITERAL_LINE.match(element)
    return int(sized.group(1)) if sized else None


def _padded_read_width(concat: str) -> int | None:
    """Read width of {pad, value, pad}: 32 minus constant padding on both ends."""
    pads = [_literal_width(part) for part in split_top_level(concat.strip()[1:-1])]
    lead = trail = 0
    while pads and pads[0] is not None:
        lead += pads.pop(0)
    while pads and pads[-1] is not None:
        trail += pads.pop()
    width = 32 - lead - trail
    return width if pads and (lead or trail) and width > 0 else None


def _if_register_writes(body: str) -> list[tuple[str, str, str]]:
    """(name, hi, lo) per register write inside ``if (wr_task_addr == `X) …``
    blocks, including ``begin … end`` bodies whose first statement is not a
    register write (a write-enable line, a counter, a comment).  The write
    register's name is the macro in the if condition, matching WRITE/WRITE_IF.

    A plain ``if (…) p <= i_st_wr_data;`` (no begin) is matched by WRITE_IF
    already; this pass handles the begin/end form where the old regex could
    only see a write at the very first statement."""
    found: list[tuple[str, str, str]] = []

    def _word_at(position: int) -> str:
        end = position
        while end < len(body) and (body[end].isalnum() or body[end] == "_"):
            end += 1
        return body[position:end]

    for match in re.finditer(r"\bif\s*\((?:[^;]*?" + _WR_ADDR + r"[^;]*?)\)\s*begin\b", body, re.S):
        # _WR_ADDR's own capture group is the register macro name
        name = match.group(1)
        depth, cursor = 1, match.end()
        while cursor < len(body) and depth:
            # keyword boundary: the char before must not be a word char
            boundary = cursor == 0 or not (body[cursor - 1].isalnum() or body[cursor - 1] == "_")
            if boundary:
                word = _word_at(cursor)
                if word == "begin":
                    depth += 1
                    cursor += 5
                    continue
                if word == "end":
                    depth -= 1
                    cursor += 3
                    continue
            cursor += 1
        if depth:
            continue   # unbalanced: leave WRITE_IF's own handling
        block = body[match.end():cursor - 1]
        for write in _WR_IF_ASSIGN.finditer(block):
            hi, lo = write.group(2), write.group(3)
            found.append((name, hi or "", lo or ""))
    return found


def _write_widths(body: str) -> dict[str, int | None]:
    """Written bit span per register; None = full-word write (no slice)."""
    spans: dict[str, tuple[int, int] | None] = {}
    for name, hi, lo in [*WRITE.findall(body), *WRITE_IF.findall(body),
                         *_if_register_writes(body)]:
        span = None
        if hi:
            first, second = int(hi), int(lo if lo else hi)
            span = (min(first, second), max(first, second))
        if name in spans:
            previous = spans[name]
            # Several slices of one register ([15:0] and [31:16]) cover their union.
            span = None if previous is None or span is None else (min(previous[0], span[0]), max(previous[1], span[1]))
        spans[name] = span
    return {name: (span[1] - span[0] + 1 if span else None) for name, span in spans.items()}


def parse_decode_file(text: str) -> list[dict]:
    """Registers implemented by one ps_rw_pl_reg module: read case plus write ternaries/ifs.

    A read address compared outside the read mux (e.g. rd_en_d2 && rd_addr_d2 == `X
    popping a FIFO) marks that register read_side_effect."""
    body = strip_comments(text)
    writes = _write_widths(body)
    reads, widths = set(), {}
    # Every case block contributes read arms; a multi-mux decode file must not
    # silently drop the later blocks.
    for case in READ_CASE.findall(body):
        for name, concat in READ_CONCAT.findall(case):
            width = _padded_read_width(concat)
            if width:
                widths[name] = width
        for name, hi, lo in _READ_SLICE.findall(case):
            # A byte/slice output bus (o_st_rd_data[7:0]) sizes the read; the
            # concat-computed width (set above) still wins when both exist.
            widths.setdefault(name, abs(int(hi) - int(lo)) + 1)
        # Only tag-position macros name registers read by this mux.
        for labels in _READ_TAG.findall(case):
            reads.update(re.findall(r"`(\w+)", labels))
    side_effects = {first or second for first, second in READ_COMPARE.findall(READ_CASE.sub(" ", body))}
    entries = []
    for name in sorted(reads | set(writes)):
        item = {"name": name, "writable": name in writes, "readable": name in reads,
                "width": writes.get(name) or widths.get(name) or 32}
        if name in side_effects:
            item["read_side_effect"] = True
        entries.append(item)
    return entries


def top_is_plaintext(text: str) -> bool:
    return re.search(r"\bmodule\b", text) is not None


def _component_tops(folder: Path) -> list[Path]:
    candidates = sorted([*Path(folder).glob("ec_*.sv"), *Path(folder).glob("ec_*.v")])
    return [candidate for candidate in candidates
            if not candidate.stem.startswith(("ps_rw_pl_reg_", "pre_post_", "proactive_", "status_", "tim_"))]


MODULE_DECL = re.compile(r"^\s*module\s+(ec_\w+)", re.M)


def folder_module_types(folder: Path) -> list[str]:
    """Type keys a top instantiates: the `module ec_*` declared in the folder (ec_1di1do/ -> ec_1di_1do)."""
    names = []
    for path in _component_tops(folder):
        try:
            text = strip_comments(read_text_resilient(path))
        except OSError:
            continue
        names.extend(name for name in MODULE_DECL.findall(text) if name not in names)
    return names


def find_top_file(key: str, decode_path: Path) -> Path | None:
    """The component top ec_<type>.sv/.v sits next to its ps_rw_pl_reg module."""
    wanted = key.replace("_", "").lower()
    candidates = _component_tops(decode_path.parent)
    for candidate in candidates:
        if candidate.stem.replace("_", "").lower() == wanted:
            return candidate
    for candidate in candidates:   # file named differently from its module
        try:
            if key in MODULE_DECL.findall(strip_comments(read_text_resilient(candidate))):
                return candidate
        except OSError:
            continue
    return None


def harvest_type_notes(top_path: Path) -> dict:
    """Parameter comments, behavior tables and the real signal names behind PARAM registers."""
    text = read_text_resilient(top_path)
    noted = strip_block_comments(text)   # ^-anchored patterns skip // lines; block comments go
    notes = {name.upper(): note.strip() for name, note in PARAM_NOTE.findall(noted) if note.strip()}
    behaviors = {}
    for channel, comment in BHA_NUM.findall(noted):
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
    for number, expression in port_hookups(body, PARAM_HEAD):
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


def iter_declarations(body: str):
    """(keyword + modifiers, packed dims, names) per declaration in comment-free source.

    A name list runs across lines up to ';', the port list's ')' or the next
    declaration keyword (ANSI ports: input [7:0] a, b, input c)."""
    position = 0
    while (head := DECLARATION_HEAD.search(body, position)):
        following = DECLARATION_KEYWORD.search(body, head.end())
        limit = following.start() if following else len(body)
        depth, cursor = 0, head.end()
        while cursor < limit:
            character = body[cursor]
            if character in "([{":
                depth += 1
            elif character in ")]}":
                if not depth:
                    break
                depth -= 1
            elif character == ";" and not depth:
                break
            cursor += 1
        names = []
        for piece in split_top_level(body[head.end():cursor]):
            name = re.match(r"\s*([A-Za-z_]\w*)", piece)
            if name and name.group(1) not in DECLARATION_KEYWORDS:
                names.append(name.group(1))
        yield head.group(0).split()[:1] + head.group(1).split(), re.findall(r"\[[^\]]*\]", head.group(2)), names
        position = max(cursor, head.end())


def _packed_width(dims: list[str]) -> int | None:
    """[3:0][7:0] -> 32; no range -> 1 bit; parameterized ranges stay unknown."""
    width = 1
    for dim in dims:
        numbers = re.fullmatch(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", dim)
        if not numbers:
            return None
        width *= abs(int(numbers.group(1)) - int(numbers.group(2))) + 1
    return width


def harvest_signal_widths(text: str) -> dict[str, int | None]:
    """Declared bit width per signal name in one module source.

    None marks a name whose width is not a plain number ([W-1:0]) or that is
    declared with different widths in different scopes."""
    widths: dict[str, int | None] = {}
    for _, dims, names in iter_declarations(strip_comments(text)):
        width = _packed_width(dims)
        for key in names:
            widths[key] = width if widths.get(key, width) == width else None
    return widths


ASSIGN = re.compile(r"\bassign\s+([^=;]+?)\s*=\s*([^;]+?)\s*;")
_PLAIN_REF = re.compile(r"^~?\s*([A-Za-z_]\w*)\s*(?:\[\s*(\d+)\s*\])?$")
_SIGNAL_REF = re.compile(r"\b([A-Za-z_]\w*)\b(?:\s*\[\s*(\d+)\s*\])?")


def module_ports(body: str) -> set[str]:
    """input/output/inout names of the module(s) in comment-free source."""
    return {name for modifiers, _, names in iter_declarations(body)
            if modifiers[0] in ("input", "output", "inout") for name in names}


def _plain_refs(expression: str) -> list[tuple[str, int | None]] | None:
    """(name, bit) per element of a plain alias (one ref or a concat of refs, MSB first); None otherwise."""
    expression = expression.strip()
    parts = (split_top_level(expression[1:-1]) if expression.startswith("{") and expression.endswith("}")
             else [expression])
    refs = []
    for part in parts:
        match = _PLAIN_REF.match(part.strip())
        if not match:
            return None
        refs.append((match.group(1), int(match.group(2)) if match.group(2) else None))
    return refs


def _select_bit(refs: list, bit: int | None, widths: dict) -> list[tuple[str, int | None]]:
    """Element(s) of a concat carrying `bit`; all of them when bit is None."""
    if bit is None:
        return refs
    if len(refs) == 1:
        name, own = refs[0]
        return [(name, bit if own is None else own)]
    cursor = 0
    for name, own in reversed(refs):
        width = 1 if own is not None else widths.get(name)
        if not width:
            return []
        if cursor <= bit < cursor + width:
            return [(name, own if own is not None else (bit - cursor if width > 1 else None))]
        cursor += width
    return []


def wired_ports(expression: str, ports: set[str], assigns: list, widths: dict) -> list[str]:
    """Module ports behind a hookup expression, following plain assign aliases
    (``assign di_i = i_pos`` / ``assign {o_dri2, o_dri1} = do_o``) only."""
    found: list[str] = []
    visited: set = set()

    def trace(name: str, bit: int | None, depth: int):
        if (name, bit) in visited:
            return
        visited.add((name, bit))
        if name in ports:
            if name not in found:
                found.append(name)
            return
        if depth >= 4:
            return
        def same(refs):
            return len(refs) == 1 and refs[0][0] == name and (refs[0][1] is None or bit in (None, refs[0][1]))

        for lhs, rhs, two_way in assigns:
            if same(lhs):
                targets = _select_bit(rhs, bit if lhs[0][1] is None else None, widths)   # name driven by rhs
            elif two_way and same(rhs):
                targets = _select_bit(lhs, bit if rhs[0][1] is None else None, widths)   # name drives lhs
            else:
                continue
            for target, target_bit in targets:
                trace(target, target_bit, depth + 1)

    for name, bit in _SIGNAL_REF.findall(SIZED_LITERAL.sub(" ", expression)):
        trace(name, int(bit) if bit else None, 0)
    return found


NET_DECL_ASSIGN = re.compile(r"\b(?:wire|logic)\b((?:\s+(?:signed|unsigned)\b)?\s*(?:\[[^\]]*\]\s*)*)([^;]*=[^;]*);")


def _ternary_branches(expression: str) -> list[str] | None:
    """[a, b] of ``cond ? a : b`` (single level); None for anything else."""
    if expression.count("?") != 1:
        return None
    rest, depth = expression.split("?", 1)[1], 0
    for index, character in enumerate(rest):
        depth += 1 if character in "[{(" else -1 if character in "]})" else 0
        if character == ":" and not depth:
            return [rest[:index], rest[index + 1:]]
    return None


def plain_assigns(body: str) -> list[tuple[list, list]]:
    """(lhs refs, rhs refs, two_way) per plain alias in comment-free source: continuous
    assigns and net declaration assigns (``wire a = b[3]``).  Each ``c ? x : y`` branch
    is a one-way entry: traced from lhs to its drivers, never back."""
    pairs = list(ASSIGN.findall(body))
    for _, declared in NET_DECL_ASSIGN.findall(body):
        for piece in split_top_level(declared):
            if "=" in piece:
                pairs.append(tuple(piece.split("=", 1)))
    result = []
    for lhs, rhs in pairs:
        left = _plain_refs(lhs)
        if not left:
            continue
        right = _plain_refs(rhs)
        if right:
            result.append((left, right, True))
            continue
        for branch in _ternary_branches(rhs) or []:
            if (right := _plain_refs(branch)):
                result.append((left, right, False))
    return result


def element_width(element: str, widths: dict | None = None) -> int | None:
    """Width of one concat element: None when it can't be determined.

    Plain identifiers take their declared width from `widths`; one missing
    there is unknown (None). Without a widths map (names only) it counts 1 bit."""
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
        return 1 if widths is None else widths.get(element)
    return None


def extract_concat_fields(expression: str, widths: dict | None = None) -> list[dict]:
    """Bit-field layout of a Verilog concat {a, b, c}: a takes the highest bits.

    Sized literals count toward the width (padding) but carry no name; plain
    identifiers take their declared width from `widths` (no layout if undeclared),
    indexed selections keep their range."""
    expression = expression.strip()
    if expression.startswith("{") and expression.endswith("}"):
        expression = expression[1:-1]
    elements = []
    total = 0
    for raw in split_top_level(expression):
        element = raw.strip()
        width = element_width(element, widths)
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
    for number, expression in port_hookups(body, DEBUG_HEAD):
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


_LOOKUP = object()


def build_type_entry(key: str, decode_path: Path, defines: dict[str, int],
                     rtl_root: Path | None = None, top_file=_LOOKUP) -> tuple[dict, list[str], list[str]]:
    """One catalog type entry from a decode file; returns (entry, forced_irq, warnings).

    top_file: the caller's find_top_file result (None = no top); looked up when omitted."""
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
        if item.get("read_side_effect"):
            entry["read_side_effect"] = True   # UI/monitor never auto-read it
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
    if top_file is _LOOKUP:
        top_file = find_top_file(key, decode_path)
    if top_file is None:
        warnings.append(f"{key}: 未找到组件顶层 ec_*.sv，参数注释与行为表未收割")
        return entry, forced, warnings
    top_text = read_text_resilient(top_file)
    if not top_is_plaintext(top_text):
        # Encrypted/binary top: nothing below would be reliable (every PARAM would look unwired).
        warnings.append(f"{top_file.name}: 组件顶层不可识别（可能被加密），参数注释与信号未收割")
        return entry, forced, warnings
    harvested = harvest_type_notes(top_file)
    if harvested["notes"]:
        entry["notes"] = harvested["notes"]
    if harvested["behaviors"]:
        entry["behaviors"] = harvested["behaviors"]
    signals = harvested.get("signals", {})
    top_body = strip_comments(top_text)
    widths = harvest_signal_widths(top_text)
    # A hookup that survives comment stripping means the register is wired at all.
    param_hookups = dict(port_hookups(top_body, PARAM_HEAD))
    wired = set(param_hookups)
    debug_hookups = dict(port_hookups(top_body, DEBUG_HEAD))
    signed_names = harvest_signed_signals(top_file.parent)
    ports, assigns = module_ports(top_body), plain_assigns(top_body)
    for item in registers:
        if item["name"].startswith("PARAM") and item["name"][5:] in param_hookups:
            # Component ports behind the register: the top import maps them to board DI/DO.
            reached = wired_ports(param_hookups[item["name"][5:]], ports, assigns, widths)
            if reached:
                item["ports"] = reached
        signal = signals.get(item["name"])
        if signal:
            item["signal"] = signal
            # A register fed by a signed wire displays as two's complement in DEC.
            roots = [part.split("[")[0] for part in signal.split("/")]
            if any(root in signed_names or root in PULSE_SIGNED_SIGNALS for root in roots):
                item["signed"] = True
        elif item["name"].startswith("PARAM") and item["name"][5:] not in wired:
            item["unwired"] = True
        # Concat hookups carry a per-bit layout: show every wire as a field.
        if item["name"].startswith("PARAM"):
            expression = param_hookups.get(item["name"][5:], "")
            if expression.lstrip().startswith("{"):
                fields = extract_concat_fields(expression, widths)
                if fields:
                    item["fields"] = fields
        elif item["name"].startswith("DEBUG_REG"):
            expression = debug_hookups.get(item["name"][-1], "")
            if expression.lstrip().startswith("{"):
                fields = extract_concat_fields(expression, widths)
                if fields:
                    item["fields"] = fields
    entry["debug_notes"] = debug_notes_for(key, registers, harvest_debug_snapshots(top_body))
    return entry, forced, warnings


# 脉冲域位置族信号：RTL 内部按有符号处理（如 s_move_tgt = rserv_target_pulse
# 转 signed），即使端口声明省略 signed 关键字也应按补码显示。
PULSE_SIGNED_SIGNALS = {"rserv_target_pulse", "rserv_step_pulse"}


def harvest_signed_signals(folder: Path) -> set[str]:
    """Signal names declared signed (ports or wire/reg/logic, ranged or not) in the folder."""
    folder = Path(folder)
    names = set()
    for path in sorted([*folder.glob("*.sv"), *folder.glob("*.v")]):
        try:
            body = strip_comments(read_text_resilient(path))
        except OSError:
            continue
        for modifiers, _, declared in iter_declarations(body):
            if "signed" in modifiers:
                names.update(declared)
    return names


CATALOG_SCHEMA = 8


def find_reg_addr_map(folder: Path) -> Path | None:
    """Walk up from a component folder to its include_files/reg_addr_pl.vh."""
    folder = Path(folder).resolve()
    for ancestor in [folder, *list(folder.parents)[:8]]:
        candidate = ancestor / "include_files" / "reg_addr_pl.vh"
        if candidate.is_file():
            return candidate
    return None


def parse_component_folder(folder: Path) -> tuple[dict, dict]:
    """Runtime 更新控件: re-parse one component folder into a catalog type entry."""
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"文件夹不存在：{folder}")
    decode_candidates = sorted([*folder.glob("ps_rw_pl_reg_*.sv"), *folder.glob("ps_rw_pl_reg_*.v")])
    if not decode_candidates:
        raise ValueError("文件夹中未找到 ps_rw_pl_reg_*.sv/.v 寄存器解码文件。")
    decode_path = decode_candidates[0]
    notices = []
    if len(decode_candidates) > 1:
        notices.append(f"文件夹内有多个寄存器解码文件（{', '.join(path.name for path in decode_candidates)}），"
                       f"使用 {decode_path.name}")
    text = read_text_resilient(decode_path)
    if not any(marker in text for marker in PLAINTEXT_MARKERS):
        raise ValueError(f"解码文件不可识别（可能被加密或格式变化）：{decode_path.name}")
    defines_path = find_reg_addr_map(folder)
    if defines_path is None:
        raise ValueError("未在组件文件夹及其上级找到 include_files/reg_addr_pl.vh（寄存器地址表）。")
    defines = parse_define_table(read_text_resilient(defines_path), notices)
    if not defines:
        raise ValueError(f"寄存器地址表为空或不可读：{defines_path}")
    types = folder_module_types(folder)
    key = types[0] if types else folder.name
    top_file = find_top_file(key, decode_path)
    entry, forced, warnings = build_type_entry(key, decode_path, defines, top_file=top_file)
    info = {"decode_file": str(decode_path), "defines": str(defines_path),
            "top_file": str(top_file) if top_file is not None else "", "forced_irq": forced,
            "warnings": notices + warnings, "imported_from": str(folder), "types": types}
    entry["imported_from"] = str(folder)
    entry["imported_at"] = datetime.now().astimezone().isoformat(timespec="seconds")   # survives backup/restore, unlike mtime
    return entry, info


def find_component_folders(root: Path, *, max_depth: int = 32,
                           max_folders: int = 20_000,
                           max_files: int = 200_000) -> list[Path]:
    """Every folder under root that carries its own ps_rw_pl_reg module (batch 更新控件)."""
    root = Path(root)
    if not root.is_dir() or max_depth < 0 or max_folders <= 0 or max_files <= 0:
        return []
    folders: set[Path] = set()
    pending: list[tuple[Path, int]] = [(root, 0)]
    visited = files_seen = 0
    while pending and visited < max_folders and files_seen < max_files:
        folder, depth = pending.pop()
        visited += 1
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for path in entries:
            if files_seen >= max_files:
                break
            try:
                # Do not recurse through user supplied links/junctions.
                if path.is_symlink():
                    continue
                if path.is_dir():
                    if depth < max_depth and visited + len(pending) < max_folders:
                        pending.append((path, depth + 1))
                    continue
                files_seen += 1
                if path.suffix.lower() in (".sv", ".v") and path.name.startswith("ps_rw_pl_reg_"):
                    folders.add(path.parent)
            except OSError:
                continue
    return sorted(folders)
