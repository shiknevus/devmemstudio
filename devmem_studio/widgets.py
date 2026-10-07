# -*- coding: utf-8 -*-
from PySide6.QtCore import QEvent, Qt, QRectF, Signal, QObject, QRunnable
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIntValidator, QPainter, QPen, QTextCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
                               QFrame, QComboBox, QLineEdit, QPlainTextEdit, QSizePolicy, QStyle,
                               QStyleOptionComboBox, QStylePainter)
from . import completion
from .theme import icon


def label(text, name=None):
    item = QLabel(text)
    if name:
        item.setObjectName(name)
    if name == "badge":
        item.setFixedHeight(32)   # matches QPushButton/QComboBox: min-height 30 + 1px border
    return item


class ElidedLabel(QLabel):
    """Plain-text label that elides visually but retains full text and tooltip."""
    def __init__(self, text="", name=None, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self._extra_tooltip = ""
        self.setTextFormat(Qt.PlainText)
        if name:
            self.setObjectName(name)
        if name == "badge":
            self.setFixedHeight(32)
        self.setMinimumWidth(0)
        self.setText(text)

    def text(self):
        return self._full_text

    def setText(self, text):
        self._full_text = str(text)
        self.setAccessibleName(self._full_text)
        self._render_text()
        self.updateGeometry()

    def setToolTip(self, text):
        self._extra_tooltip = str(text)
        self._render_text()

    def _render_text(self):
        if not hasattr(self, "_full_text"):
            return
        # contentsRect already excludes QSS padding (badges), so only margin() is left to remove.
        available = max(0, self.contentsRect().width() - self.margin() * 2)
        displayed = self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, available)
        super().setText(displayed)
        tooltip = self._full_text
        if self._extra_tooltip and self._extra_tooltip != tooltip:
            tooltip += "\n" + self._extra_tooltip
        super().setToolTip(tooltip)

    def sizeHint(self):
        hint = super().sizeHint()
        margins = self.contentsMargins()
        hint.setWidth(max(hint.width(), self.fontMetrics().horizontalAdvance(self._full_text)
                          + margins.left() + margins.right() + self.margin() * 2))
        return hint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_text()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._render_text()


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
    changed = Signal()

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
            edit.textChanged.connect(self.changed)
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


def password_field(title, echo=QLineEdit.Password, extra=None):
    """QLineEdit with the eyes action drawn inside the field's trailing edge.

    addAction renders the icon inside the input box (integrated, not an attached
    button); clicking it toggles mask/plain and swaps the icon for feedback.
    `extra` (e.g. a remember checkbox) sits right-aligned on the title line."""
    container = QWidget()
    outer = QVBoxLayout(container)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(5)
    title_label = label(title, "muted")
    if extra is None:
        outer.addWidget(title_label)
    else:
        # Let the title row fit both the label and the extra control after styling.
        # A checkbox's indicator can be taller than the label's initial size hint.
        outer.addLayout(row(title_label, 1, extra, spacing=6))
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


def divider(name="divider"):
    line = QFrame()
    line.setObjectName(name)
    line.setFixedHeight(1)
    return line


def restyle(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class ComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class ResizeAwareWidget(QWidget):
    """Container that reports its own width changes (splitters move it without a window resize)."""
    resized = Signal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.resized.emit()


class ElidedComboBox(ComboBox):
    """Closed state elides long item text; the popup widens to show it in full."""
    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter.drawComplexControl(QStyle.CC_ComboBox, option)
        text_rect = self.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, self)
        option.currentText = self.fontMetrics().elidedText(option.currentText, Qt.ElideRight,
                                                           max(0, text_rect.width() - 2))
        painter.drawControl(QStyle.CE_ComboBoxLabel, option)

    def showPopup(self):
        widest = max((self.fontMetrics().horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0)
        self.view().setMinimumWidth(max(self.width(), widest + 36))
        super().showPopup()


class ChannelSection(QFrame):
    """One connection channel: status dot, name, target summary and its connect
    button on a single line; the settings body folds away below it."""
    expandedChanged = Signal(bool)

    def __init__(self, name, body, action, parent=None):
        super().__init__(parent)
        self.setObjectName("channel")
        self.body = body
        self.toggle = QPushButton()
        self.toggle.setObjectName("channelToggle")
        self.toggle.setCheckable(True)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setFocusPolicy(Qt.NoFocus)
        self.toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.toggle.setAccessibleName(f"{name} 连接设置")
        inner = QHBoxLayout(self.toggle)
        inner.setContentsMargins(8, 0, 4, 0)
        inner.setSpacing(7)
        self.dot = QLabel()
        self.dot.setObjectName("statusDot")
        self.dot.setFixedSize(8, 8)
        self.name = label(name, "channelName")
        self.summary = ElidedLabel("", "channelSummary")
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.chevron = QLabel()
        self.chevron.setFixedSize(14, 14)
        for part in (self.dot, self.name, self.summary, self.chevron):
            part.setAttribute(Qt.WA_TransparentForMouseEvents)   # the whole line is the toggle
            inner.addWidget(part, 1 if part is self.summary else 0)
        header = QHBoxLayout()
        header.setContentsMargins(4, 5, 8, 5)
        header.setSpacing(6)
        header.addWidget(self.toggle, 1)
        header.addWidget(action)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(body)
        self.toggle.toggled.connect(self._apply)
        self._apply(False)

    def _apply(self, expanded):
        self.body.setVisible(expanded)
        self.chevron.setPixmap(icon("down" if expanded else "chevron", "#8FA4B6", 14).pixmap(14, 14))
        self.expandedChanged.emit(expanded)

    def is_expanded(self):
        return self.toggle.isChecked()

    def set_expanded(self, expanded):
        self.toggle.setChecked(bool(expanded))

    def set_state(self, state, text):
        if self.dot.property("state") != state:
            self.dot.setProperty("state", state)
            restyle(self.dot)
        self.dot.setToolTip(text)

    def set_summary(self, text, tooltip=""):
        self.summary.setText(text)
        self.toggle.setToolTip((tooltip or text) + "\n点击展开 / 收起连接参数")


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


# Raw (serial) terminal: keys as a vt102 keyboard sends them; Backspace is ^? like the board's tty.
RAW_KEYS = {Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r", Qt.Key_Backspace: b"\x7f", Qt.Key_Tab: b"\t",
            Qt.Key_Backtab: b"\x1b[Z", Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B", Qt.Key_Right: b"\x1b[C",
            Qt.Key_Left: b"\x1b[D", Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F", Qt.Key_Delete: b"\x1b[3~",
            Qt.Key_Insert: b"\x1b[2~", Qt.Key_Escape: b"\x1b"}
RAW_CTRL_SYMBOLS = {Qt.Key_BracketLeft: b"\x1b", Qt.Key_Backslash: b"\x1c", Qt.Key_BracketRight: b"\x1d",
                    Qt.Key_At: b"\x00", Qt.Key_Space: b"\x00"}


def raw_key_bytes(key, modifiers, text):
    """Bytes a serial terminal sends for one key press, or None for local keys."""
    ctrl = bool(modifiers & Qt.ControlModifier)
    alt = bool(modifiers & Qt.AltModifier)
    if ctrl and not alt:
        if Qt.Key_A <= key <= Qt.Key_Z:
            return bytes([key - Qt.Key_A + 1])
        return RAW_CTRL_SYMBOLS.get(key)
    if key in RAW_KEYS:
        return RAW_KEYS[key]
    if text and all(char.isprintable() for char in text):
        data = text.encode("utf-8")
        return b"\x1b" + data if alt else data
    return None


class TerminalView(QPlainTextEdit):
    """Log view whose trailing line is an editable shell prompt.

    Everything above the prompt is history: selectable but not editable — any
    typed key is redirected into the prompt line. Enter submits the line to the
    `execute` callback; Up/Down walk the command history; Tab asks the
    `complete(line, reply)` callback for candidates. Non-session views
    (tail log / system) stay plain read-only via set_interactive(False).
    set_raw(sender) turns it into a character-mode serial terminal instead."""
    PROMPT = "❯ "

    def __init__(self, execute, complete=None, parent=None):
        super().__init__(parent)
        self._execute = execute
        self._complete = complete
        self._completion_token = 0
        self._interactive = False
        self._raw = None
        self._history = []
        self._history_index = 0
        self.setReadOnly(True)   # read-only until a session view turns it interactive

    # -- state ---------------------------------------------------------------
    def set_interactive(self, on):
        self._raw = None
        self._interactive = on
        self.setReadOnly(not on)
        if on:
            self.ensure_prompt()

    def is_interactive(self):
        return self._interactive

    # -- raw (serial) mode -----------------------------------------------------
    def set_raw(self, sender):
        """Character mode: every key goes to `sender(bytes)` and the board echoes it."""
        self._interactive = False
        self._raw = sender
        self.setReadOnly(True)
        # Keyboard-selectable keeps the caret visible on the board's cursor.
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

    def is_raw(self):
        return self._raw is not None

    def raw_reset(self, text, column, follow=True):
        self.setPlainText(text)
        self._place_raw_cursor(column, follow)

    def raw_update(self, committed, current, column, follow=True):
        """Replace the live (last) line with newly committed lines plus the new live line."""
        bar = self.verticalScrollBar()
        held = None if follow else (bar.value(), self.horizontalScrollBar().value())
        cursor = QTextCursor(self.document().lastBlock())
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        cursor.insertText("\n".join([*committed, current]))
        self._place_raw_cursor(column, follow, held)

    def _place_raw_cursor(self, column, follow, held=None):
        bar = self.verticalScrollBar()
        if held is None and not follow:
            held = (bar.value(), self.horizontalScrollBar().value())
        cursor = self.textCursor()
        if not cursor.hasSelection():   # never steal a selection the user is copying
            block = self.document().lastBlock()
            cursor.setPosition(block.position() + min(column, block.length() - 1))
            self.setTextCursor(cursor)
        if follow:
            bar.setValue(bar.maximum())
        elif held is not None:
            bar.setValue(held[0])
            self.horizontalScrollBar().setValue(held[1])

    def _raw_send(self, data):
        if data:
            self._raw(data)

    def _raw_paste(self):
        text = QGuiApplication.clipboard().text()
        if text:
            self._raw_send(text.replace("\r\n", "\r").replace("\n", "\r").encode("utf-8"))

    def _raw_key(self, event):
        key, modifiers = event.key(), event.modifiers()
        ctrl = bool(modifiers & Qt.ControlModifier)
        shift = bool(modifiers & Qt.ShiftModifier)
        if key in (Qt.Key_PageUp, Qt.Key_PageDown) and not ctrl:
            bar = self.verticalScrollBar()   # scroll history locally
            bar.setValue(bar.value() + (bar.pageStep() if key == Qt.Key_PageDown else -bar.pageStep()))
            return
        if ctrl and key == Qt.Key_C and (shift or self.textCursor().hasSelection()):
            self.copy()   # Ctrl+C copies a selection, otherwise it is ^C for the board
            return
        if (ctrl and key == Qt.Key_V) or (shift and key == Qt.Key_Insert):
            self._raw_paste()
            return
        data = raw_key_bytes(key, modifiers, event.text())
        if data is None:
            super().keyPressEvent(event)
            return
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        self._raw_send(data)

    def focusNextPrevChild(self, next):
        if self._raw is not None:
            return False   # Tab belongs to the board's shell
        return super().focusNextPrevChild(next)

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

    def _cursor_in_input(self):
        cursor = self.textCursor()
        start = self._prompt_start()
        return cursor.position() >= start and cursor.anchor() >= start

    def _keep_cursor_in_prompt(self):
        """Caret/selection inside the input stays put; anywhere else jumps to the end."""
        if not self._cursor_in_input():
            self._force_cursor_to_prompt()

    def _caret_offset(self):
        return self.textCursor().position() - self._prompt_start()

    def _set_caret_offset(self, offset):
        cursor = self.textCursor()
        cursor.setPosition(self._prompt_start() + max(0, min(offset, len(self._prompt_input()))))
        self.setTextCursor(cursor)

    # -- output --------------------------------------------------------------
    def append_before_prompt(self, text, fmt=None):
        """Insert log output above the prompt line (prompt stays last)."""
        block = self.document().lastBlock()
        cursor = QTextCursor(block)
        cursor.beginEditBlock()
        # Insert at the prompt's start; the trailing newline pushes the prompt down.
        text = text if text.endswith("\n") else text + "\n"
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
        self._force_cursor_to_prompt()

    def history(self):
        return list(self._history)

    def _request_completion(self):
        if not self._complete:
            return
        self._keep_cursor_in_prompt()
        self._completion_token += 1
        token, line, caret = self._completion_token, self._prompt_input(), self._caret_offset()
        self._complete(line[:caret], lambda candidates: self._apply_completion(token, line, caret, candidates))

    def _apply_completion(self, token, line, caret, candidates):
        # Drop a late reply once the user has typed on or moved the caret.
        if (token != self._completion_token or not self._interactive
                or self._prompt_input() != line or self._caret_offset() != caret):
            return
        before, after = line[:caret], line[caret:]
        new_before, listing = completion.apply(before, candidates)
        if new_before != before:
            self._set_prompt_input(new_before + after)
            self._set_caret_offset(len(new_before))
        elif listing:
            shown = listing[:100]
            more = f"  …（共 {len(listing)} 项）" if len(listing) > len(shown) else ""
            self.append_before_prompt("  ".join(shown) + more)
            self._set_caret_offset(caret)
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def keyPressEvent(self, event):
        if self._raw is not None:
            self._raw_key(event)
            return
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
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            self._request_completion()
            return
        if key in (Qt.Key_Up, Qt.Key_Down):
            self._history_step(key == Qt.Key_Up)
            return
        if key in (Qt.Key_PageUp, Qt.Key_PageDown):
            bar = self.verticalScrollBar()   # scroll history without moving the caret
            bar.setValue(bar.value() + (bar.pageStep() if key == Qt.Key_PageDown else -bar.pageStep()))
            return
        # Everything else edits the prompt line only; the caret may sit anywhere in it.
        self._keep_cursor_in_prompt()
        start = self._prompt_start()
        cursor = self.textCursor()
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if key == Qt.Key_Home:
            cursor.setPosition(start, QTextCursor.KeepAnchor if shift else QTextCursor.MoveAnchor)
            self.setTextCursor(cursor)
            return
        if key in (Qt.Key_Backspace, Qt.Key_Left) and not cursor.hasSelection() and cursor.position() <= start:
            return   # keep the prompt marker intact
        if key == Qt.Key_Backspace and event.modifiers() & Qt.ControlModifier and not cursor.hasSelection():
            cursor.movePosition(QTextCursor.PreviousWord, QTextCursor.KeepAnchor)
            if cursor.position() < start:
                cursor.setPosition(start, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            return
        super().keyPressEvent(event)
        cursor = self.textCursor()
        if cursor.position() < start:   # Ctrl+Left / PageUp must not leave the input
            anchor = cursor.anchor() if cursor.anchor() >= start else start
            cursor.setPosition(anchor)
            cursor.setPosition(start, QTextCursor.KeepAnchor)
            self.setTextCursor(cursor)
        if not self._prompt_present():
            self.ensure_prompt()

    def insertFromMimeData(self, source):
        """Paste lands in the prompt as a single line (history stays intact)."""
        if self._raw is not None:
            text = source.text().replace("\r\n", "\r").replace("\n", "\r")
            self._raw_send(text.encode("utf-8"))
            return
        if not self._interactive:
            return
        text = source.text().replace(" ", " ").replace("\r", " ").replace("\n", " ")
        if not text:
            return
        self._keep_cursor_in_prompt()
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
    serial_data = Signal(int, str)
    serial_lost = Signal(int, str)
    serial_uboot = Signal(int, bool)
