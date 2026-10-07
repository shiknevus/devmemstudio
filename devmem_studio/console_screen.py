# -*- coding: utf-8 -*-
"""Line model behind the raw serial terminal: enough VT102 to follow shell line editing."""
from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ScreenBuffer:
    """Committed lines plus the live line the cursor sits on.

    The board believes it drives an 80-column vt102 (serial has no size
    negotiation), so cursor moves and readline's wrap handling are computed on
    that grid; each logical line is still kept as one string and the widget
    soft-wraps it to its own width."""

    def __init__(self, width=80, max_lines=5000):
        self.width = width
        self.max_lines = max_lines
        self.lines = []
        self.chars = []
        self.cur = 0
        self._wrap = False
        self._esc = ""
        self._saved = 0
        self._new = []

    # -- views ---------------------------------------------------------------
    def current(self):
        """Live line padded out to the cursor (a cursor past the text sits on blanks)."""
        text = "".join(self.chars)
        return text + " " * (self.cur - len(text)) if self.cur > len(text) else text

    def cursor(self):
        return self.cur

    def text(self):
        return "\n".join(self.lines + [self.current().rstrip()])

    def clear(self):
        """Drop history; the live line (usually the prompt) stays."""
        self.lines.clear()

    # -- input ---------------------------------------------------------------
    def note(self, text):
        """Local annotation on its own line; returns the newly committed lines."""
        self._new = []
        if self.chars:
            self._commit()
        self.lines.append(text)
        self._new.append(text)
        self._trim()
        return self._new

    def feed(self, text):
        """Apply board output; returns the lines committed by it, oldest first."""
        self._new = []
        position = 0
        while position < len(text):
            if self._esc:
                position = self._escape(text, position)
                continue
            match = _CONTROL.search(text, position)
            end = match.start() if match else len(text)
            if end > position:
                self._put(text[position:end])
            if not match:
                break
            position = end + 1
            char = match.group()
            if char == "\n":
                self._line_feed()
            elif char == "\r":
                self._wrap = False
                self.cur -= self.cur % self.width
            elif char == "\b":
                self._wrap = False
                if self.cur % self.width:
                    self.cur -= 1
            elif char == "\t":
                start = self.cur - self.cur % self.width
                self._wrap = False
                self.cur = start + min(self.width - 1, (self.cur - start) // 8 * 8 + 8)
            elif char == "\x1b":
                self._esc = char
        self._trim()
        return self._new

    # -- internals -----------------------------------------------------------
    def _put(self, text):
        width = self.width
        while text:
            if self._wrap:
                self._wrap = False
                self.cur += 1
            room = width - self.cur % width
            part, text = text[:room], text[room:]
            if self.cur > len(self.chars):
                self.chars.extend(" " * (self.cur - len(self.chars)))
            end = self.cur + len(part)
            self.chars[self.cur:end] = part
            if end % width == 0:
                self.cur, self._wrap = end - 1, True   # pending wrap, like xenl terminals
            else:
                self.cur = end

    def _row(self):
        start = self.cur - self.cur % self.width
        return start, start + self.width

    def _line_feed(self):
        self._wrap = False
        _, end = self._row()
        if end < len(self.chars):   # still inside a wrapped logical line
            self.cur += self.width
        else:
            self._commit()

    def _commit(self):
        line = "".join(self.chars).rstrip(" ")
        self.lines.append(line)
        self._new.append(line)
        self.chars = []
        self.cur = 0
        self._wrap = False

    def _trim(self):
        if len(self.lines) > self.max_lines:
            del self.lines[:-self.max_lines]

    def _escape(self, text, position):
        """Consume one escape sequence (possibly split across feeds)."""
        while position < len(text):
            char = text[position]
            position += 1
            self._esc += char
            sequence = self._esc
            if len(sequence) > 64:
                self._esc = ""
                return position
            if len(sequence) == 2:
                if char in "[]" or " " <= char <= "/":
                    continue   # CSI / OSC / charset designator (ESC ( B)
                self._esc = ""
                self._simple_escape(char)
                return position
            if sequence[1] == "]":
                if char == "\x07" or sequence.endswith("\x1b\\"):
                    self._esc = ""
                    return position
                continue
            if sequence[1] != "[":
                self._esc = ""
                return position
            if "@" <= char <= "~":
                self._esc = ""
                self._csi(sequence[2:-1], char)
                return position
        return position

    def _simple_escape(self, char):
        if char == "7":
            self._saved = self.cur
        elif char == "8":
            self.cur, self._wrap = self._saved, False
        elif char == "E":
            self.cur -= self.cur % self.width
            self._line_feed()
        elif char == "M":
            self._csi("1", "A")

    def _csi(self, params, final):
        if params.startswith(("?", ">", "=")):
            return   # private modes
        numbers = [int(part) if part.isdigit() else 0 for part in params.split(";")] if params else []
        first = numbers[0] if numbers else 0
        count = max(1, first)
        width = self.width
        start, end = self._row()
        column = self.cur - start
        if final in "ABCDG":
            self._wrap = False
        if final == "A":
            self.cur = max(column, self.cur - width * count)
        elif final == "B":
            last_row = max(0, len(self.chars) - 1) // width * width
            self.cur = max(self.cur, min(self.cur + width * count, last_row + column))
        elif final == "C":
            self.cur = start + min(width - 1, column + count)
        elif final == "D":
            self.cur = start + max(0, column - count)
        elif final == "G":
            self.cur = start + min(width - 1, count - 1)
        elif final == "K":
            self._erase_row(first, start, end)
        elif final == "J" and first == 0:
            del self.chars[self.cur:]
        elif final == "P":
            self._delete_chars(count, end)
        elif final == "@":
            self._insert_blanks(count, end)
        elif final == "X":
            stop = min(self.cur + count, end, len(self.chars))
            self.chars[self.cur:stop] = " " * max(0, stop - self.cur)
        # SGR, absolute addressing, scrolling regions and modes are ignored

    def _erase_row(self, mode, start, end):
        last_row = end >= len(self.chars)
        if mode == 0:
            if last_row:
                del self.chars[self.cur:]
            else:
                self.chars[self.cur:end] = " " * (end - self.cur)
        elif mode == 1:
            stop = min(self.cur + 1, len(self.chars))
            self.chars[start:stop] = " " * max(0, stop - start)
        elif mode == 2:
            if last_row:
                del self.chars[start:]
            else:
                self.chars[start:end] = " " * self.width

    def _delete_chars(self, count, end):
        if end >= len(self.chars):
            del self.chars[self.cur:self.cur + count]
            return
        row = self.chars[self.cur:end]
        del row[:count]
        row.extend(" " * (end - self.cur - len(row)))
        self.chars[self.cur:end] = row

    def _insert_blanks(self, count, end):
        if self.cur > len(self.chars):
            return
        last_row = end >= len(self.chars)
        self.chars[self.cur:self.cur] = " " * count
        if last_row:
            del self.chars[end:]   # cells pushed past the margin fall off
        else:
            del self.chars[end:end + count]
