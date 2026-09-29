# -*- coding: utf-8 -*-
"""Bash-style Tab completion for the terminal prompt.

Pure string logic: the window runs `remote_script` on the board (SSH or
serial) and feeds the parsed names back through `apply`."""
from __future__ import annotations

import os
import shlex

# Shell builtins never show up in $PATH; offered at command position.
SHELL_BUILTINS = ("cd", "echo", "export", "pwd", "exit", "source", "alias", "unset", "set",
                  "type", "kill", "jobs", "test", "read", "exec", "wait", "umask", "true", "false")

_SPECIAL = set(" \t'\"\\$&;|<>()*?[]!#`")
_COMMAND_SEPARATORS = "|;&("
_MAX_NAMES = 200


def escape(text: str) -> str:
    return "".join("\\" + ch if ch in _SPECIAL else ch for ch in text)


def unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            i += 1
        out.append(text[i])
        i += 1
    return "".join(out)


def split_word(line: str):
    """(head, raw_word): the word under completion is after the last unescaped space."""
    i = len(line)
    while i > 0:
        if line[i - 1] == " " and not (i >= 2 and line[i - 2] == "\\"):
            break
        i -= 1
    return line[:i], line[i:]


def is_command_position(head: str, word: str) -> bool:
    stripped = head.rstrip()
    return "/" not in word and (not stripped or stripped[-1] in _COMMAND_SEPARATORS)


def local_candidates(word: str, command_pos: bool, history=()):
    if not command_pos:
        return []
    names = set(SHELL_BUILTINS) | {"devmem"}
    names.update(line.split()[0] for line in history if line.split())
    return sorted(name for name in names if name.startswith(word))


def remote_script(word: str, command_pos: bool):
    """One-line POSIX/busybox-ash script printing candidate names, or None."""
    if word.startswith("$"):
        return None   # variables: not completed
    if command_pos:
        prefix = shlex.quote(word) if word else ""
        return ("(IFS=:; for d in $PATH; do for f in \"$d\"/" + prefix + "*; do "
                "[ -f \"$f\" ] && [ -x \"$f\" ] && printf '%s\\n' \"${f##*/}\"; done; done)"
                f" 2>/dev/null | head -n {_MAX_NAMES}")
    slash = word.rfind("/")
    dir_part, prefix = word[:slash + 1], word[slash + 1:]
    if dir_part.startswith("~/"):
        rest = dir_part[2:]
        dir_expr = '"$HOME"/' + (shlex.quote(rest) if rest else "")
    else:
        dir_expr = shlex.quote(dir_part) if dir_part else ""
    pattern = dir_expr + (shlex.quote(prefix) if prefix else "") + "*"
    return ("(for f in " + pattern + "; do [ -e \"$f\" ] || [ -L \"$f\" ] || continue; "
            "if [ -d \"$f\" ]; then printf '%s/\\n' \"${f##*/}\"; else printf '%s\\n' \"${f##*/}\"; fi; done)"
            f" 2>/dev/null | head -n {_MAX_NAMES}")


def parse_remote(output: str, word: str):
    """Board names -> full candidate words (the typed directory part kept as-is)."""
    dir_part = word[:word.rfind("/") + 1]
    return sorted({dir_part + line for line in (output or "").splitlines() if line.strip()})


def apply(line: str, candidates):
    """(new_line, listing): complete the last word; listing is set when ambiguous."""
    head, raw = split_word(line)
    word = unescape(raw)
    matches = sorted({c for c in candidates if c.startswith(word)})
    if not matches:
        return line, None
    if len(matches) == 1:
        only = matches[0]
        return head + escape(only) + ("" if only.endswith("/") else " "), None
    common = os.path.commonprefix(matches)
    if len(common) > len(word):
        return head + escape(common), None
    dir_part = word[:word.rfind("/") + 1]
    return line, [m[len(dir_part):] for m in matches]
