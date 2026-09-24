# -*- coding: utf-8 -*-
from PySide6.QtCore import QEvent, Qt, QRectF, Signal, QObject, QRunnable
from PySide6.QtGui import QColor, QFont, QIntValidator, QPainter, QPen, QTextCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
                               QFrame, QComboBox, QLineEdit, QPlainTextEdit, QSizePolicy)
from .theme import icon


def label(text, name=None):
    item = QLabel(text)
    if name:
        item.setObjectName(name)
    if name == "badge":
        item.setFixedHeight(27)
    return item


def button(text, slot=None, name=None, glyph=None):
    item = QPushButton(text)
    item.setCursor(Qt.PointingHandCursor)
    if name:
        item.setObjectName(name)
    if glyph:
        item.setIcon(icon(glyph, "#FFFFFF" if name == "primary" else "#B64242" if name == "danger" else "#718397"))
    if slot:
        item.clicked.connect(slot)
    return item


def row(*widgets, spacing=8):
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        if isinstance(widget, QWidget):
            layout.addWidget(widget)
        else:
            layout.addStretch(widget if isinstance(widget, int) else 1)
    return layout


def field(title, widget):
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    layout.addWidget(label(title, "muted"))
    layout.addWidget(widget)
    if widget.minimumWidth() == widget.maximumWidth():
        box.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
    return box


class IpAddressField(QWidget):
    """Four 0-255 octets separated by dots; each octet is its own input.

    Typing into one octet moves the caret to the next when it fills, and the
    surrounding QSS treats the whole group as one input (the rounded border
    wraps all four octets with the dots between them)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        # Plain QWidget subclasses don't paint their QSS box (border/background)
        # unless this attribute is set — without it the field has no visible frame.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.octets = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 0, 9, 0)
        lay.setSpacing(2)
        for i in range(4):
            edit = QLineEdit()
            edit.setObjectName("ipOctet")
            edit.setMaxLength(3)
            edit.setFixedWidth(30)
            edit.setAlignment(Qt.AlignCenter)
            edit.setValidator(QIntValidator(0, 255))
            edit.textEdited.connect(lambda text, idx=i: self._on_edited(idx, text))
            # QSS has no :focus-within; toggle a dynamic property on group focus so
            # the whole field highlights like a plain QLineEdit.
            edit.installEventFilter(self)
            lay.addWidget(edit)
            self.octets.append(edit)
            if i < 3:
                dot = QLabel("·")
                dot.setObjectName("ipDot")
                dot.setFixedWidth(5)
                dot.setAlignment(Qt.AlignCenter)
                lay.addWidget(dot)
        self.setFocusProxy(self.octets[0])

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            focused = any(edit.hasFocus() for edit in self.octets)
            if focused != bool(self.property("focused")):
                self.setProperty("focused", focused)
                restyle(self)
        return super().eventFilter(watched, event)

    def _on_edited(self, idx, text):
        # Auto-advance to the next octet once this one is full (3 digits).
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 3 and idx < 3:
            self.octets[idx + 1].setFocus()
            self.octets[idx + 1].setCursorPosition(0)

    def text(self):
        parts = [edit.text().strip() for edit in self.octets]
        if not any(parts):
            return ""
        return ".".join(parts)

    def setText(self, value):
        value = value or ""
        parts = value.split(".") if value else ["", "", "", ""]
        while len(parts) < 4:
            parts.append("")
        for edit, part in zip(self.octets, parts[:4]):
            edit.setText(part.strip())

    def clear(self):
        for edit in self.octets:
            edit.clear()

    def setFocus(self):
        self.octets[0].setFocus()


def password_field(title, echo=QLineEdit.Password):
    """QLineEdit with the eyes action drawn inside the field's trailing edge.

    addAction renders the icon inside the input box (integrated, not an attached
    button); clicking it toggles mask/plain and swaps the icon for feedback."""
    container = QWidget()
    outer = QVBoxLayout(container)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(5)
    outer.addWidget(label(title, "muted"))
    edit = QLineEdit()
    edit.setEchoMode(echo)
    reveal = edit.addAction(icon("eye", "#9DB3C5", 17), QLineEdit.TrailingPosition)
    reveal.setToolTip("显示 / 隐藏密码")

    def toggle():
        show = edit.echoMode() == QLineEdit.Password
        edit.setEchoMode(QLineEdit.Normal if show else QLineEdit.Password)
        reveal.setIcon(icon("eyeOff" if show else "eye", "#9DB3C5", 17))
    reveal.triggered.connect(toggle)
    outer.addWidget(edit)
    return edit, container


def divider():
    line = QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


def restyle(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class ComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class DecodedFieldsView(QFrame):
    """Aligned field names, bit ranges and values; grows rows on demand."""
    def __init__(self):
        super().__init__()
        self.setObjectName("decodedFields")
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(8, 8, 8, 8)
        self.grid.setHorizontalSpacing(5)
        self.grid.setVerticalSpacing(7)
        self.grid.setColumnStretch(0, 1)
        for column, title in enumerate(("字段解析", "位段", "值")):
            heading = label(title, "eyebrow")
            heading.setAlignment((Qt.AlignLeft if column == 0 else Qt.AlignRight) | Qt.AlignVCenter)
            self.grid.addWidget(heading, 0, column)
        self.rows = []
        self.field_values = {}

    def _row_cells(self, index):
        """Create grid rows on demand so any field count fits (one per line)."""
        while len(self.rows) <= index:
            cells = (label("", "fieldName"), label("", "fieldBits"), label("", "fieldValue"))
            for column, cell in enumerate(cells):
                cell.setTextInteractionFlags(Qt.TextSelectableByMouse)
                cell.setAlignment((Qt.AlignLeft if column == 0 else Qt.AlignRight) | Qt.AlignVCenter)
                self.grid.addWidget(cell, len(self.rows) + 1, column)
            self.rows.append(cells)
        return self.rows[index]

    def set_fields(self, definitions, values):
        self.field_values = {}
        for index, definition in enumerate(definitions):
            cells = self._row_cells(index)
            for cell in cells:
                cell.setVisible(True)
            name, high, low, radix = definition
            cells[0].setText(name)
            derived = high < low   # sentinel: derived/aggregate row, not an RTL bit field
            cells[1].setText("·" if derived else f"[{high}:{low}]" if high != low else f"[{low}]")
            cells[2].setText(values.get(name, "—"))
            description = (f"{name} · 由寄存器计算得出\n每行一个派生值" if derived else
                           f"{name} · 位 [{high}:{low}] · {'十六进制' if radix == 'HEX' else '十进制'}\n每行一个 RTL 信号")
            for cell in cells:
                cell.setToolTip(description)
            cells[2].setStyleSheet("color:#2463DC;" if radix == "HEX" and name in values else
                                  "color:#23374A;" if name in values else "color:#91A0AF;")
            self.field_values[name] = cells[2]
        for cells in self.rows[len(definitions):]:
            for cell in cells:
                cell.setVisible(False)


class CommandLine(QLineEdit):
    def __init__(self):
        super().__init__()
        self.history = []
        self.position = 0

    def remember(self, command):
        if not self.history or self.history[-1] != command:
            self.history.append(command)
            self.history = self.history[-100:]
        self.position = len(self.history)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Up, Qt.Key_Down) and self.history:
            self.position = max(0, min(len(self.history), self.position + (-1 if event.key() == Qt.Key_Up else 1)))
            self.setText(self.history[self.position] if self.position < len(self.history) else "")
            return
        super().keyPressEvent(event)


class TerminalView(QPlainTextEdit):
    """Log view whose trailing line is an editable shell prompt.

    Everything above the prompt is history: selectable but not editable — any
    typed key is redirected into the prompt line. Enter submits the line to the
    `execute` callback; Up/Down walk the command history. Non-session views
    (tail log / system) stay plain read-only via set_interactive(False)."""
    PROMPT = "❯ "

    def __init__(self, execute, parent=None):
        super().__init__(parent)
        self._execute = execute
        self._interactive = False
        self._history = []
        self._history_index = 0
        self.setReadOnly(True)   # read-only until a session view turns it interactive

    # -- state ---------------------------------------------------------------
    def set_interactive(self, on):
        self._interactive = on
        self.setReadOnly(not on)
        if on:
            self.ensure_prompt()

    def is_interactive(self):
        return self._interactive

    def ensure_prompt(self):
        if not self._prompt_present():
            cursor = QTextCursor(self.document())
            cursor.movePosition(QTextCursor.End)
            if cursor.block().length() > 1:
                cursor.insertBlock()
            cursor.insertText(self.PROMPT)

    def _prompt_present(self):
        return self.document().lastBlock().text().startswith(self.PROMPT)

    def _prompt_start(self):
        return self.document().lastBlock().position() + len(self.PROMPT)

    def _force_cursor_to_prompt(self):
        cursor = self.textCursor()
        cursor.clearSelection()
        cursor.movePosition(QTextCursor.End)   # caret lands after any typed input
        self.setTextCursor(cursor)

    # -- output --------------------------------------------------------------
    def append_before_prompt(self, text, fmt=None):
        """Insert log output above the prompt line (prompt stays last)."""
        block = self.document().lastBlock()
        cursor = QTextCursor(block)
        cursor.beginEditBlock()
        cursor.insertBlock()   # split: output block lands above the prompt text
        if fmt is not None:
            cursor.insertText(text, fmt)
        else:
            cursor.insertText(text)
        cursor.endEditBlock()
        self.ensure_prompt()

    # -- input ---------------------------------------------------------------
    def _prompt_input(self):
        return self.document().lastBlock().text()[len(self.PROMPT):]

    def _set_prompt_input(self, text):
        cursor = QTextCursor(self.document().lastBlock())
        cursor.select(QTextCursor.LineUnderCursor)
        cursor.insertText(self.PROMPT + text)

    def _submit(self):
        text = self._prompt_input().strip()
        if text:
            if not self._history or self._history[-1] != text:
                self._history.append(text)
                self._history = self._history[-100:]
            self._history_index = len(self._history)
        self._set_prompt_input("")
        self._execute(text)

    def _history_step(self, up):
        if not self._history:
            return
        if up:
            self._history_index = max(0, self._history_index - 1)
        else:
            self._history_index = min(len(self._history), self._history_index + 1)
        self._set_prompt_input(self._history[self._history_index]
                               if self._history_index < len(self._history) else "")

    def keyPressEvent(self, event):
        if not self._interactive:
            super().keyPressEvent(event)
            return
        key = event.key()
        if event.modifiers() == Qt.ControlModifier and key == Qt.Key_C:
            super().keyPressEvent(event)   # copy from history stays available
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._submit()
            return
        if key in (Qt.Key_Up, Qt.Key_Down):
            self._history_step(key == Qt.Key_Up)
            return
        # Everything else edits the prompt line only.
        self._force_cursor_to_prompt()
        if key == Qt.Key_Backspace:
            cursor = self.textCursor()
            if not cursor.hasSelection() and cursor.position() <= self._prompt_start():
                return   # keep the prompt marker intact
        if key == Qt.Key_Home:
            cursor = self.textCursor()
            cursor.clearSelection()
            cursor.setPosition(self._prompt_start())
            self.setTextCursor(cursor)
            return
        super().keyPressEvent(event)
        if not self._prompt_present():
            self.ensure_prompt()

    def insertFromMimeData(self, source):
        """Paste lands in the prompt as a single line (history stays intact)."""
        if not self._interactive:
            return
        text = source.text().replace(" ", " ").replace("\r", " ").replace("\n", " ")
        if not text:
            return
        self._force_cursor_to_prompt()
        self.textCursor().insertText(text)


class BitView(QWidget):
    """Thirty-two measured bits, grouped into bytes, with no fabricated empty-state values."""
    def __init__(self):
        super().__init__()
        self.value = None
        self.setFixedHeight(100)
        self.setToolTip("从左到右：高位 → 低位。蓝色为 1，灰色为 0，短横线表示尚未读取。")

    def set_value(self, value):
        self.value = value
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        margin, gap = 1, 4
        size = (self.width() - margin * 2 - gap * 7) / 8
        font = QFont("Consolas", 9)
        painter.setFont(font)
        for group in range(4):
            y = group * 25
            for column in range(8):
                bit = 31 - group * 8 - column
                on = self.value is not None and (self.value >> bit) & 1
                rect = QRectF(margin + column * (size + gap), y + 2, size, 21)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#2463DC" if on else "#EFF3F7"))
                painter.drawRoundedRect(rect, 3, 3)
                painter.setPen(QColor("#FFFFFF" if on else "#91A0AF"))
                painter.drawText(rect, Qt.AlignCenter, str(bit) if self.value is not None else "–")
        painter.end()


class WorkerSignals(QObject):
    result = Signal(object)
    failed = Signal(str)
    finished = Signal()
    progress = Signal(object)


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.result.emit(self.function(self.signals.progress.emit))
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            self.signals.finished.emit()


class LogBridge(QObject):
    # (level, text, source): the source travels with the signal so cross-thread
    # queued emissions can never pick up another session's tag.
    message = Signal(str, str, str)
    stream = Signal(int, str)
    stream_stopped = Signal(int)
    stream_ready = Signal(int, bool)
    stream_failed = Signal(int, str)
    stream_worker_finished = Signal(int)
