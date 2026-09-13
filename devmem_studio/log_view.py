"""Wrapped, numbered logs with incremental coloring and literal search."""
from PySide6.QtCore import Qt, Signal, QTimer, QRegularExpression, QEvent, QPointF, QRectF, QSize
from PySide6.QtGui import (QColor, QFont, QPainter, QSyntaxHighlighter, QTextBlockUserData,
                          QTextCharFormat, QTextCursor, QTextDocument, QTextOption)
from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QTextEdit, QWidget


def text_format(color, background=None, bold=False):
    result = QTextCharFormat()
    result.setForeground(QColor(color))
    if background:
        result.setBackground(QColor(background))
    if bold:
        result.setFontWeight(QFont.Bold)
    return result


def expression(pattern, case_sensitive=False):
    flags = QRegularExpression.UseUnicodePropertiesOption
    if not case_sensitive:
        flags |= QRegularExpression.CaseInsensitiveOption
    return QRegularExpression(pattern, flags)


class MatchData(QTextBlockUserData):
    def __init__(self, matches):
        super().__init__()
        self.matches = matches


class LogHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.query = None
        self.levels = [
            (expression(r"\b(?:FATAL|CRITICAL|ERROR|ERR|FAIL(?:ED|URE)?|EXCEPTION|PANIC)\b|错误|失败|异常|崩溃"), "#FF8E94"),
            (expression(r"\b(?:WARN(?:ING)?|ALERT|TIMEOUT|TIMED\s+OUT)\b|警告|告警|超时"), "#F0C478"),
            (expression(r"\b(?:SUCCESS|PASS(?:ED)?|OK)\b|成功|通过"), "#7EDBBD"),
            (expression(r"\b(?:DEBUG|TRACE|VERBOSE)\b|调试"), "#8DA4B8"),
        ]
        self.tokens = [
            (expression(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?\b|\b\d{4}-\d{2}-\d{2}\b"), text_format("#809CB3")),
            (expression(r"\b0x[0-9a-f]+\b"), text_format("#87BCFF")),
            (expression(r"\b(?:INFO|NOTICE|SYSTEM)\b"), text_format("#8FCBEB", bold=True)),
        ]
        self.match_format = text_format("#FFE1A0", "#495044")

    def set_query(self, query, case_sensitive):
        self.query = expression(QRegularExpression.escape(query), case_sensitive) if query else None
        self.rehighlight()

    def highlightBlock(self, text):
        # Qt offsets count UTF-16 code units, including for characters outside the BMP.
        for regex, color in self.levels:
            if regex.match(text).hasMatch():
                self.setFormat(0, len(text.encode("utf-16-le")) // 2, text_format(color))
                break
        for regex, fmt in self.tokens:
            matches = regex.globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)
        for regex, color in self.levels:
            matches = regex.globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), text_format(color, bold=True))
        found = []
        if self.query:
            matches = self.query.globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                start, length = match.capturedStart(), match.capturedLength()
                self.setFormat(start, length, self.match_format)
                found.append((start, length))
        self.setCurrentBlockUserData(MatchData(found))


class LogSearchEdit(QLineEdit):
    next_requested = Signal()
    previous_requested = Signal()
    leave_requested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            signal = self.previous_requested if event.modifiers() & Qt.ShiftModifier else self.next_requested
            signal.emit()
            event.accept()
        elif event.key() == Qt.Key_Escape:
            self.clear()
            self.leave_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setAccessibleName("日志行号")
        self.setToolTip("当前缓存的原始行号，与 Ctrl+G 跳转一致；自动换行的续行不重复编号。")

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)

    def wheelEvent(self, event):
        self.editor.wheelEvent(event)


class LogView(QPlainTextEdit):
    matches_changed = Signal(int, int)
    location_changed = Signal(int, int)
    browse_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("boardConsole")
        self.setReadOnly(True)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setMaximumBlockCount(5000)
        self.highlighter = LogHighlighter(self.document())
        self.current_match = QTextCursor()
        self._editing = False
        self._moving = False
        self._case_sensitive = False
        self._query = ""
        self._line_numbers_enabled = True
        self.line_numbers = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_width)
        self.updateRequest.connect(self._update_line_numbers)
        self.cursorPositionChanged.connect(self.line_numbers.update)
        self._update_line_number_width()
        # Count cached per-block matches at most five times per second while tail is active.
        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(200)
        self._count_timer.timeout.connect(self.refresh_counts)
        self.document().contentsChanged.connect(self._content_changed)
        self.cursorPositionChanged.connect(self._cursor_changed)
        self.verticalScrollBar().sliderMoved.connect(self._scroll_browsed)

    def line_number_area_width(self):
        if not self._line_numbers_enabled:
            return 0
        digits = max(2, len(str(self.blockCount())))
        return self.fontMetrics().horizontalAdvance("9" * digits) + 16

    def _update_line_number_width(self, *_):
        width = self.line_number_area_width()
        if self.viewportMargins().left() != width:
            self.setViewportMargins(width, 0, 0, 0)
        self.line_numbers.setVisible(self._line_numbers_enabled)
        self._position_line_numbers()
        self.line_numbers.update()

    def _position_line_numbers(self):
        # Follow the actual viewport so stylesheet padding and DPI scaling stay aligned.
        viewport = self.viewport().geometry()
        width = self.line_number_area_width()
        self.line_numbers.setGeometry(viewport.x() - width, viewport.y(), width, viewport.height())

    def _update_line_numbers(self, rect, dy):
        if dy:
            self.line_numbers.scroll(0, dy)
        else:
            self.line_numbers.update(0, rect.y(), self.line_numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "line_numbers"):
            self._position_line_numbers()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, "line_numbers") and event.type() in (
                QEvent.FontChange, QEvent.ApplicationFontChange, QEvent.StyleChange):
            self._update_line_number_width()

    def paint_line_numbers(self, event):
        painter = QPainter(self.line_numbers)
        painter.fillRect(event.rect(), QColor("#152938"))
        painter.setFont(self.font())
        metrics = self.fontMetrics()
        width = self.line_numbers.width()
        block = self.firstVisibleBlock()
        current = self.textCursor().blockNumber()
        while block.isValid():
            top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
            height = self.blockBoundingRect(block).height()
            if top > event.rect().bottom():
                break
            if block.isVisible() and top + height >= event.rect().top():
                first_line = block.layout().lineAt(0) if block.layout().lineCount() else None
                line_top = top + first_line.y() if first_line else top
                line_height = first_line.height() if first_line else metrics.height()
                baseline = line_top + (first_line.ascent() if first_line else metrics.ascent())
                active = block.blockNumber() == current
                if active:
                    painter.fillRect(QRectF(0, line_top, width, line_height), QColor("#203A4B"))
                painter.setPen(QColor("#F0C478" if active else "#809CB3"))
                number = str(block.blockNumber() + 1)
                painter.drawText(QPointF(width - 8 - metrics.horizontalAdvance(number), baseline), number)
            block = block.next()
        painter.setPen(QColor("#2D485C"))
        painter.drawLine(width - 1, event.rect().top(), width - 1, event.rect().bottom())
        painter.end()

    def set_wrapping(self, enabled):
        mode = QPlainTextEdit.WidgetWidth if enabled else QPlainTextEdit.NoWrap
        if self.lineWrapMode() != mode:
            self.setLineWrapMode(mode)

    def set_line_numbers_visible(self, enabled):
        self._line_numbers_enabled = bool(enabled)
        self._update_line_number_width()

    def _content_changed(self):
        if not self._count_timer.isActive():
            self._count_timer.start()

    def _cursor_changed(self):
        if not self._editing and not self._moving:
            self.current_match = QTextCursor()
            self.setExtraSelections([])
            self.refresh_counts()

    def _scroll_browsed(self, value):
        if value < self.verticalScrollBar().maximum():
            self.browse_requested.emit()

    def wheelEvent(self, event):
        if event.angleDelta().y() > 0:
            self.browse_requested.emit()
        super().wheelEvent(event)

    def mousePressEvent(self, event):
        self.browse_requested.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Up, Qt.Key_PageUp, Qt.Key_Home):
            self.browse_requested.emit()
        super().keyPressEvent(event)

    def append_data(self, text, follow=True):
        self._editing = True
        try:
            cursor = QTextCursor(self.document())
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(text)
        finally:
            self._editing = False
        if follow:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def set_search(self, query, case_sensitive=False):
        self._query, self._case_sensitive = query, case_sensitive
        self.current_match = QTextCursor()
        self.setExtraSelections([])
        self.highlighter.set_query(query, case_sensitive)
        self.refresh_counts()

    def find_match(self, forward=True, include_current=False):
        if not self._query:
            return False
        self.browse_requested.emit()
        anchor = self.current_match if self.current_match.hasSelection() else self.textCursor()
        start = anchor.selectionStart() if include_current or not forward else anchor.selectionEnd()
        flags = QTextDocument.FindFlags()
        if not forward:
            flags |= QTextDocument.FindBackward
        if self._case_sensitive:
            flags |= QTextDocument.FindCaseSensitively
        found = self.document().find(self._query, start, flags)
        if found.isNull():
            edge = 0 if forward else self.document().characterCount() - 1
            found = self.document().find(self._query, edge, flags)
        if found.isNull():
            self.current_match = QTextCursor()
            self.setExtraSelections([])
            self.refresh_counts()
            return False
        found.setKeepPositionOnInsert(True)
        self.current_match = found
        self._moving = True
        try:
            self.setTextCursor(found)
            self.centerCursor()
        finally:
            self._moving = False
        self._paint_current_match()
        self.refresh_counts()
        return True

    def _paint_current_match(self):
        selections = []
        if self.current_match.hasSelection():
            selection = QTextEdit.ExtraSelection()
            selection.cursor = self.current_match
            selection.format = text_format("#142635", "#F0C478", bold=True)
            selections.append(selection)
        self.setExtraSelections(selections)

    def refresh_counts(self):
        self._count_timer.stop()
        total, current = 0, 0
        if self._query:
            target = self.current_match.selectionStart() if self.current_match.hasSelection() else -1
            target_end = self.current_match.selectionEnd()
            block = self.document().begin()
            while block.isValid():
                data = block.userData()
                matches = data.matches if isinstance(data, MatchData) else ()
                if block.position() <= target < block.position() + block.length():
                    for index, (start, length) in enumerate(matches):
                        if block.position() + start == target and target + length == target_end:
                            current = total + index + 1
                            break
                total += len(matches)
                block = block.next()
            if not current:
                self.current_match = QTextCursor()
            self._paint_current_match()
        self.matches_changed.emit(current, total)
        self.location_changed.emit(self.textCursor().blockNumber() + 1, self.blockCount())

    def jump_to_line(self, number):
        self.browse_requested.emit()
        block = self.document().findBlockByNumber(max(0, min(number - 1, self.blockCount() - 1)))
        self.setTextCursor(QTextCursor(block))
        self.centerCursor()
        self.refresh_counts()

    def clear(self):
        self.current_match = QTextCursor()
        self.setExtraSelections([])
        super().clear()
        self.refresh_counts()
