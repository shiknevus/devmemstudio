# -*- coding: utf-8 -*-
from PySide6.QtCore import Qt, QRectF, Signal, QObject, QRunnable
from PySide6.QtGui import QColor, QFont, QPainter, QPen
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
        item.setIcon(icon(glyph, "#FFFFFF" if name == "primary" else "#718397"))
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
            cells[1].setText(f"[{high}:{low}]" if high != low else f"[{low}]")
            cells[2].setText(values.get(name, "—"))
            description = f"{name} · 位 [{high}:{low}] · {'十六进制' if radix == 'HEX' else '十进制'}\n每行一个 RTL 信号"
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
    message = Signal(str, str)
    stream = Signal(int, str)
    stream_stopped = Signal(int)
    stream_ready = Signal(int, bool)
    stream_failed = Signal(int, str)
    stream_worker_finished = Signal(int)
