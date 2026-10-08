# -*- coding: utf-8 -*-
"""Register monitor: a few registers sampled on the board and plotted against time."""
from array import array
from bisect import bisect_left, bisect_right
import copy
import csv
from datetime import datetime
import math
import threading
import time

from PySide6.QtCore import Qt, QEvent, QObject, QPointF, QRectF, QSignalBlocker, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QDialog, QWidget, QFrame, QLabel, QLayout, QHBoxLayout, QVBoxLayout, QSplitter,
                               QListWidget, QListWidgetItem,
                               QLineEdit, QSpinBox, QCheckBox, QFileDialog, QSizePolicy,
                               QStyledItemDelegate, QStyle)

from .core import (MONITOR_MIN_INTERVAL_MS, MONITOR_MAX_REGISTERS, MONITOR_STALE_TIMEOUT_S,
                   normalize_monitor_settings, parse_addr)
from .theme import icon
from .widgets import ElidedLabel, label, button, row, restyle, Worker

SERIES_COLORS = ("#2463DC", "#168578", "#B57518", "#C44848", "#7A4FC4", "#1F8FB3", "#6E7F1F", "#C2477F")
SAMPLE_LIMIT = 200_000   # per run; the oldest quarter is dropped beyond it
RANGE_BLOCK = 32
SAMPLER_LABELS = {"shell": "devmem 循环（板端 CPU 占用较高）", "demo": "演示数据"}
STATE_COLORS = {"idle": "#A3B1BF", "starting": "#2463DC", "running": "#2E9D5B"}
STATUS_COLORS = {"warn": "#D59A2E", "error": "#C84B4B"}


def to_signed(value):
    return value - (1 << 32) if value >= 0x80000000 else value


class MonitorBuffer:
    """Shared time axis plus raw/signed value columns. A failed read holds the previous
    value and is indexed separately, so min()/max() run on plain C arrays."""

    def __init__(self, count, limit=SAMPLE_LIMIT):
        self.limit = limit
        self.t0 = None
        self.times = array("d")
        self.raw = [array("d") for _ in range(count)]
        self.signed = [array("d") for _ in range(count)]
        self.failures = [array("q") for _ in range(count)]   # sample indices
        self.total = 0
        self.failed = 0
        self.revision = 0
        self._ranges = [([], [], [], []) for _ in range(count)]

    def __len__(self):
        return len(self.times)

    def extend(self, samples):
        for stamp, values in samples:
            if self.t0 is None:
                self.t0 = stamp
            index = len(self.times)
            self.times.append(stamp - self.t0)
            for column, value in enumerate(values):
                raw = self.raw[column]
                if value is None:
                    self.failures[column].append(index)
                    self.failed += 1
                    value = int(raw[-1]) if raw else 0
                raw.append(value)
                signed = to_signed(value)
                self.signed[column].append(signed)
                for summary, candidate, minimum in zip(self._ranges[column], (value, value, signed, signed),
                                                        (True, False, True, False)):
                    if index % RANGE_BLOCK == 0:
                        summary.append(candidate)
                    elif (minimum and candidate < summary[-1]) or (not minimum and candidate > summary[-1]):
                        summary[-1] = candidate
        self.total += len(samples)
        self.revision += 1
        if len(self.times) > self.limit:
            self._drop(len(self.times) - self.limit * 3 // 4)

    def _drop(self, count):
        del self.times[:count]
        for column in range(len(self.raw)):
            del self.raw[column][:count]
            del self.signed[column][:count]
            self.failures[column] = array("q", (i - count for i in self.failures[column] if i >= count))
            summaries = ([], [], [], [])
            for start in range(0, len(self.times), RANGE_BLOCK):
                for values, low, high in ((self.raw[column], summaries[0], summaries[1]),
                                          (self.signed[column], summaries[2], summaries[3])):
                    chunk = values[start:start + RANGE_BLOCK]
                    low.append(min(chunk))
                    high.append(max(chunk))
            self._ranges[column] = summaries

    def column(self, index, signed=False):
        return (self.signed if signed else self.raw)[index]

    def extrema(self, column, begin, end, signed=False):
        """Exact extrema: inspect small boundary fragments and cached whole blocks."""
        values = self.column(column, signed)
        first, last = (begin + RANGE_BLOCK - 1) // RANGE_BLOCK, end // RANGE_BLOCK
        if first >= last:
            chunk = values[begin:end]
            return min(chunk), max(chunk)
        offset = 2 if signed else 0
        low = min(self._ranges[column][offset][first:last])
        high = max(self._ranges[column][offset + 1][first:last])
        for chunk in (values[begin:first * RANGE_BLOCK], values[last * RANGE_BLOCK:end]):
            if chunk:
                low, high = min(low, min(chunk)), max(high, max(chunk))
        return low, high

    @property
    def dropped(self):
        return self.total - len(self.times)

    def period(self, span=200):
        """Median seconds per sample over the latest span samples, so a pause does not read as lag."""
        times = self.times[-span:]
        if len(times) < 2:
            return None
        gaps = sorted(b - a for a, b in zip(times, times[1:]))
        return gaps[len(gaps) // 2]


def nice_step(raw):
    magnitude = 10 ** math.floor(math.log10(raw))
    return next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw)


def format_seconds(value, step):
    decimals = max(0, -math.floor(math.log10(step)) if step < 1 else 0)
    return f"{value:.{decimals}f} s"


class MonitorPlot(QWidget):
    """One lane per register on a shared time axis; step traces, per-pixel min/max envelope."""
    zoom_changed = Signal(float)
    markers_changed = Signal(object)
    lane_clicked = Signal(int)
    LABEL_WIDTH = 220
    AXIS_HEIGHT = 26
    MIN_ZOOM = 0.25
    MAX_ZOOM = 64.0
    ZOOM_STEP = 1.25

    def __init__(self, parent=None):
        super().__init__(parent)
        self.buffer = None
        self.names = []
        self.offsets = []
        self.zoom_factor = 1.0
        self._view_range = None
        self.signed = False
        self.highlight = None   # lane of the register picked in the list
        self._hover = None
        self.markers = [None, None]
        self._next_marker = 0
        self._drag_marker = None
        self._trace_cache = {}
        self._cache_key = None
        self.setMouseTracking(True)
        self.setMinimumSize(420, 220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setToolTip("单击图线依次放置 A/B 标记，拖动标记调整位置，右键清除标记。\n"
                        "单击左侧寄存器名称高亮该曲线，并在列表中定位。\n"
                        "按住 Ctrl 并滚动鼠标滚轮，以鼠标位置为中心缩放时间轴；点击 Zoom 查看全局图表。")

    def set_series(self, buffer, names, offsets=()):
        self.clear_markers()
        self.buffer, self.names = buffer, list(names)
        self.highlight = None
        self._view_range = None
        self.offsets = list(offsets) if offsets else [None] * len(self.names)
        self.setMinimumHeight(max(220, len(self.names) * 52 + max(0, len(self.names) - 1) * 8
                                  + self.AXIS_HEIGHT + 6))
        self.update()

    def time_range(self):
        if self._view_range is not None:
            return self._view_range
        times = self.buffer.times if self.buffer else array("d")
        end = times[-1] if times else 0.0
        earliest = times[0] if times else 0.0
        span = max(end - earliest, 1.0) / self.zoom_factor
        begin = max(earliest, end - span)
        return begin, max(end, begin + span)

    def set_zoom(self, factor, anchor_x=None):
        factor = max(self.MIN_ZOOM, min(self.MAX_ZOOM, factor))
        if anchor_x is None:
            self._view_range = None
        else:
            if factor == self.zoom_factor:
                return
            begin, end = self.time_range()
            lane = self.lanes()[0]
            fraction = max(0.0, min(1.0, (anchor_x - lane.left()) / lane.width()))
            anchor = begin + fraction * (end - begin)
            span = (end - begin) * self.zoom_factor / factor
            begin = anchor - fraction * span
            self._view_range = (begin, begin + span)
        if factor != self.zoom_factor:
            self.zoom_factor = factor
            self.zoom_changed.emit(factor)
        self.update()

    def zoom(self, steps, anchor_x=None):
        self.set_zoom(self.zoom_factor * self.ZOOM_STEP ** max(-32, min(32, steps)), anchor_x)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            steps = event.angleDelta().y() / 120 or event.pixelDelta().y() / 60
            lane = self.lanes()[0]
            if steps and lane.left() <= event.position().x() <= lane.right():
                self._hover = event.position().x()
                self.zoom(steps, event.position().x())
                event.accept()
                return
        super().wheelEvent(event)

    def lanes(self):
        count = max(1, len(self.names))
        top, bottom, gap = 6.0, self.height() - self.AXIS_HEIGHT, 8.0
        height = max(8.0, (bottom - top - gap * (count - 1)) / count)
        left, right = float(self.LABEL_WIDTH), self.width() - 12.0
        return [QRectF(left, top + i * (height + gap), max(10.0, right - left), height) for i in range(count)]

    @staticmethod
    def trace(times, values, first, start, span, rect, y_of, extrema=None, last=None):
        """Step polyline from sample `first` on; past 2 samples per pixel each column keeps
        its first/min/max/last so short pulses survive the decimation."""
        width = max(1, int(rect.width()))
        last = len(times) if last is None else last
        scale = rect.width() / span
        points = []
        if last - first <= width * 2:
            previous = None
            for i in range(first, last):
                x = rect.left() + (times[i] - start) * scale
                y = y_of(values[i])
                if previous is not None:
                    points.append(QPointF(x, previous))
                points.append(QPointF(x, y))
                previous = y
            return points
        low = first
        for column in range(width + 1):
            high = bisect_right(times, start + (column + 1) / scale, low, last)
            if high > low:
                bottom, top = extrema(low, high) if extrema else (min(values[low:high]), max(values[low:high]))
                x = rect.left() + column
                if points:
                    points.append(QPointF(x, points[-1].y()))
                points += [QPointF(x, y_of(values[low])), QPointF(x, y_of(bottom)),
                           QPointF(x, y_of(top)), QPointF(x, y_of(values[high - 1]))]
                low = high
        return points

    def value_text(self, column, index):
        raw = int(self.buffer.raw[column][index])
        return f"0x{raw:08X} · {to_signed(raw) if self.signed else raw}"

    def header_texts(self, column):
        offset = self.offsets[column]
        address = f"+0x{offset:03X}" if offset is not None else "—"
        value = self.value_text(column, len(self.buffer) - 1) if self.buffer is not None and len(self.buffer) else "—"
        return self.names[column], address, value

    def clear_markers(self):
        changed = any(stamp is not None for stamp in self.markers)
        self.markers = [None, None]
        self._next_marker = 0
        self._drag_marker = None
        if changed:
            self.markers_changed.emit(tuple(self.markers))
            self.update()

    def _set_marker(self, marker, stamp):
        if self.markers[marker] != stamp:
            self.markers[marker] = stamp
            self.markers_changed.emit(tuple(self.markers))
            self.update()

    def set_highlight(self, column):
        if column != self.highlight:
            self.highlight = column
            self.update()

    def _in_plot(self, position):
        return bool(self.names) and any(lane.contains(position) for lane in self.lanes())

    def _gutter_lane_at(self, position):
        if not self.names or not 0 <= position.x() < self.LABEL_WIDTH:
            return None
        return next((i for i, lane in enumerate(self.lanes()[:len(self.names)])
                     if lane.top() <= position.y() <= lane.bottom()), None)

    def _sample_time_at(self, position):
        if not self._in_plot(position) or self.buffer is None or not len(self.buffer):
            return None
        lane = self.lanes()[0]
        begin, end = self.time_range()
        stamp = begin + (position.x() - lane.left()) / lane.width() * (end - begin)
        times = self.buffer.times
        if stamp < times[0] or stamp > times[-1]:
            return None
        index = bisect_left(times, stamp)
        if index and (index == len(times) or stamp - times[index - 1] <= times[index] - stamp):
            index -= 1
        return times[index]

    def mousePressEvent(self, event):
        position = event.position()
        if event.button() == Qt.RightButton and self._in_plot(position):
            self.clear_markers()
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            lane = self._gutter_lane_at(position)
            if lane is not None:
                self.lane_clicked.emit(lane)
                event.accept()
                return
            stamp = self._sample_time_at(position)
            if stamp is not None:
                lane = self.lanes()[0]
                begin, end = self.time_range()
                nearby = [(abs(lane.left() + (value - begin) / (end - begin) * lane.width() - position.x()), i)
                          for i, value in enumerate(self.markers) if value is not None and begin <= value <= end]
                closest = min(nearby, default=(math.inf, None))
                if self.markers[self._next_marker] is None or closest[0] > 6:
                    marker = self._next_marker
                    self._next_marker = 1 - marker
                else:
                    marker = closest[1]
                self._drag_marker = marker
                self._set_marker(marker, stamp)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_marker is not None:
            stamp = self._sample_time_at(event.position())
            if stamp is not None:
                self._set_marker(self._drag_marker, stamp)
            self._drag_marker = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        self._hover = event.position().x()
        if self._gutter_lane_at(event.position()) is None:
            self.unsetCursor()
        else:
            self.setCursor(Qt.PointingHandCursor)
        if self._drag_marker is not None and event.buttons() & Qt.LeftButton:
            stamp = self._sample_time_at(event.position())
            if stamp is not None:
                self._set_marker(self._drag_marker, stamp)
        self.update()

    def leaveEvent(self, event):
        self._hover = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        if not self.names:
            painter.setPen(QColor("#91A0AF"))
            painter.drawText(self.rect(), Qt.AlignCenter, "在左侧勾选寄存器，点「开始监视」绘制曲线")
            return
        painter.setRenderHint(QPainter.Antialiasing)
        start, end = self.time_range()
        span = end - start
        lanes = self.lanes()
        times = self.buffer.times if self.buffer else array("d")
        first = max(0, bisect_right(times, start) - 1)   # the sample held into view
        last = bisect_right(times, end)
        cache_key = (id(self.buffer), self.buffer.revision if self.buffer else 0, self.signed,
                     start, end, self.width(), self.height())
        if cache_key != self._cache_key:
            self._cache_key = cache_key
            self._trace_cache.clear()
        mono, small = QFont("Consolas", 9), QFont(self.font())
        small.setPointSize(8)
        step = nice_step(span / max(1.0, lanes[0].width() / 110))
        ticks = [step * k for k in range(math.ceil(start / step), math.floor(end / step) + 1)]
        hover = None
        if self._hover is not None and times and lanes[0].left() <= self._hover <= lanes[0].right():
            at = start + (self._hover - lanes[0].left()) / lanes[0].width() * span
            if times[0] <= at <= times[-1]:
                hover = bisect_right(times, at) - 1
        for column, (lane, name) in enumerate(zip(lanes, self.names)):
            color = QColor(SERIES_COLORS[column % len(SERIES_COLORS)])
            selected = column == self.highlight
            if selected:
                tint = QColor(color)
                tint.setAlpha(18)
                painter.setPen(Qt.NoPen)
                painter.setBrush(tint)
                painter.drawRoundedRect(QRectF(2, lane.top(), self.LABEL_WIDTH - 8, lane.height()), 4, 4)
                tint.setAlpha(8)
                painter.setBrush(tint)
            else:
                painter.setPen(QPen(QColor("#DEE5EC"), 1))
                painter.setBrush(QColor("#FAFCFE"))
            painter.drawRect(lane)
            painter.setPen(QPen(QColor("#EEF2F6"), 1))
            for tick in ticks:
                x = lane.left() + (tick - start) / span * lane.width()
                painter.drawLine(QPointF(x, lane.top() + 1), QPointF(x, lane.bottom() - 1))
            gutter = QRectF(8, lane.top(), self.LABEL_WIDTH - 16, lane.height())
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(gutter.left(), gutter.top() + 5, 4, max(4.0, lane.height() - 10)), 2, 2)
            painter.setPen(QColor("#23374A"))
            painter.setFont(self.font())
            text_rect = gutter.adjusted(10, 2, 0, 0)
            header_name, header_address, header_value = self.header_texts(column)
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignTop,
                             painter.fontMetrics().elidedText(header_name, Qt.ElideRight, int(text_rect.width())))
            painter.setFont(mono)
            painter.setPen(QColor("#718397"))
            painter.drawText(text_rect.adjusted(0, 17, 0, 0), Qt.AlignLeft | Qt.AlignTop, header_address)
            painter.setPen(color)
            painter.drawText(text_rect.adjusted(0, 34, 0, 0), Qt.AlignLeft | Qt.AlignTop, header_value)
            values = self.buffer.column(column, self.signed) if self.buffer else array("d")
            if not times or not last or start > times[-1]:
                continue
            cached = self._trace_cache.get(column)
            low, high = (cached[:2] if cached else self.buffer.extrema(column, first, last, self.signed))
            data_low, data_high = low, high
            if high == low:
                low, high = low - 1, high + 1
            pad = (high - low) * 0.1
            bottom_value, top_value = low - pad, high + pad
            def y_of(value):
                return lane.bottom() - (value - bottom_value) / (top_value - bottom_value) * lane.height()
            painter.setFont(small)
            painter.setPen(QColor("#91A0AF"))
            if lane.height() >= 30:
                painter.drawText(QRectF(lane.left() + 4, lane.top() + 1, lane.width() - 8, 14),
                                 Qt.AlignRight | Qt.AlignTop, f"{data_high:.0f}")
                painter.drawText(QRectF(lane.left() + 4, lane.bottom() - 15, lane.width() - 8, 14),
                                 Qt.AlignRight | Qt.AlignBottom, f"{data_low:.0f}")
            painter.save()
            painter.setClipRect(lane.adjusted(1, 1, -1, -1))
            trace_end = min(len(times), last + 1)   # next point completes the held step at the right edge
            dense = trace_end - first > lane.width() * 2
            # Dense envelopes repeatedly retrace a vertical pixel column. Qt's
            # antialiased wide-pen stroker is costly for these degenerate segments.
            painter.setRenderHint(QPainter.Antialiasing, not dense)
            painter.setPen(QPen(color, 1.0 if dense else 1.4))
            painter.setBrush(Qt.NoBrush)
            if cached:
                polygon, marks = cached[2:]
            else:
                polygon = QPolygonF(self.trace(times, values, first, start, span, lane, y_of,
                                              lambda lo, hi: self.buffer.extrema(column, lo, hi, self.signed),
                                              last=trace_end))
                marks = []
                failures = self.buffer.failures[column]
                if failures:
                    width = max(1, int(lane.width()))
                    for pixel in range(width):
                        lo = bisect_left(times, start + pixel / width * span, first, last)
                        hi = bisect_right(times, start + (pixel + 1) / width * span, lo, last)
                        at = bisect_left(failures, lo)
                        if at < len(failures) and failures[at] < hi:
                            marks.append(lane.left() + pixel + 0.5)
                self._trace_cache[column] = (data_low, data_high, polygon, marks)
            painter.drawPolyline(polygon)
            failures = self.buffer.failures[column]
            if failures:
                painter.setPen(QPen(QColor("#C44848"), 1))
                for x in marks:
                    painter.drawLine(QPointF(x, lane.bottom() - 6), QPointF(x, lane.bottom() - 1))
            painter.restore()
            if hover is not None:
                x = max(lane.left(), lane.left() + (times[hover] - start) / span * lane.width())
                painter.setPen(QPen(QColor("#9CAFBD"), 1, Qt.DashLine))
                painter.drawLine(QPointF(x, lane.top()), QPointF(x, lane.bottom()))
                text = self.value_text(column, hover)
                painter.setFont(mono)
                width = painter.fontMetrics().horizontalAdvance(text) + 10
                box = QRectF(x + 6 if x + 6 + width < lane.right() else x - 6 - width,
                             lane.top() + 3, width, 17)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(255, 255, 255, 225))
                painter.drawRoundedRect(box, 3, 3)
                painter.setPen(color)
                painter.drawText(box, Qt.AlignCenter, text)
        axis = lanes[-1].bottom() + 4
        painter.setFont(small)
        painter.setPen(QColor("#718397"))
        for tick in ticks:
            x = lanes[0].left() + (tick - start) / span * lanes[0].width()
            painter.drawText(QRectF(x - 40, axis, 80, 16), Qt.AlignCenter, format_seconds(tick, step))
        if hover is not None:
            x = max(lanes[0].left(), lanes[0].left() + (times[hover] - start) / span * lanes[0].width())
            text = format_seconds(times[hover], step / 100)
            box = QRectF(x - 45, axis, 90, 16)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#203747"))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(box, Qt.AlignCenter, text)
        painter.setRenderHint(QPainter.Antialiasing)
        for marker, stamp in enumerate(self.markers):
            if stamp is None or not start <= stamp <= end:
                continue
            x = lanes[0].left() + (stamp - start) / span * lanes[0].width()
            color = QColor(("#B57518", "#7A4FC4")[marker])
            painter.setPen(QPen(color, 1.4, Qt.DashLine))
            painter.drawLine(QPointF(x, lanes[0].top()), QPointF(x, lanes[-1].bottom()))
            box = QRectF(min(x + 4, lanes[0].right() - 20), lanes[0].top() + 3, 18, 18)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(box, Qt.AlignCenter, "AB"[marker])


class _MonitorBridge(QObject):
    started = Signal(int, bool)
    samples = Signal(int, object)
    stopped = Signal(int, str)
    sampler = Signal(int, str)


class _MonitorRegisterDelegate(QStyledItemDelegate):
    """Toggle checks across the whole row, using one release for each click; mark the focused row."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.focus = None   # register address

    def paint(self, painter, option, index):
        if self.focus is not None and index.data(Qt.UserRole) == self.focus:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#E3EDFF"))
            painter.drawRoundedRect(QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
            painter.setBrush(QColor("#2463DC"))
            painter.drawRoundedRect(QRectF(option.rect.left(), option.rect.top() + 3, 3, option.rect.height() - 6), 1.5, 1.5)
            painter.restore()
        super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index):
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            flags = index.flags()
            if (event.button() != Qt.LeftButton or not option.state & QStyle.State_Enabled
                    or not flags & Qt.ItemIsEnabled or not flags & Qt.ItemIsUserCheckable
                    or not option.rect.contains(event.position().toPoint())):
                return False
            if event.type() == QEvent.MouseButtonRelease:
                checked = index.data(Qt.CheckStateRole) == Qt.Checked.value
                return model.setData(index, Qt.Unchecked.value if checked else Qt.Checked.value, Qt.CheckStateRole)
            return True
        return super().editorEvent(event, model, option, index)


class MonitorDialog(QDialog):
    """Pick registers of the current component, sample them on the board and plot them."""
    interval_changed = Signal(int)
    settings_changed = Signal(dict)

    def __init__(self, session_provider, interval_ms=10, parent=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle("寄存器监视")
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.resize(1120, 680)
        self.setMinimumSize(760, 460)
        self._session_provider = session_provider
        self.settings = normalize_monitor_settings(settings)
        self._selection_key = None
        self._epoch = 0
        self._stop = threading.Event()
        self._workers = set()
        self._dirty = False
        self.state = "idle"
        self.buffer = None
        self.monitored = []   # (name, address) of the current/last run
        self.monitored_offsets = []
        self.sampler = ""     # board-side sampler of the current/last run, see SAMPLER_LABELS
        self._last_data = None   # monotonic time of the latest samples of this run
        self._shown_pause = 0
        self.interval_ms = interval_ms
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.bridge = _MonitorBridge(self)
        self.bridge.started.connect(self._started)
        self.bridge.samples.connect(self._samples)
        self.bridge.stopped.connect(self._stopped)
        self.bridge.sampler.connect(self._sampler_known)

        layout = QVBoxLayout(self)
        layout.setVerticalSizeConstraint(QLayout.SetMinimumSize)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        self.component_label = ElidedLabel("", "muted")
        self.component_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        control_width = 110
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(MONITOR_MIN_INTERVAL_MS, 60000)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setFixedWidth(control_width)
        self.interval_spin.setValue(max(MONITOR_MIN_INTERVAL_MS, min(60000, int(interval_ms))))
        self.interval_spin.setToolTip(f"板端采样间隔，最小 {MONITOR_MIN_INTERVAL_MS} ms。aarch64 板子上由常驻采样程序"
                                      "一次映射、循环读取，CPU 占用很低；无法使用时改为每点每寄存器启动一次 devmem，"
                                      "寄存器越多、间隔越小，CPU 占用越高。跟不上时按实际速度采样并在状态栏提示。")
        self.interval_spin.valueChanged.connect(self.interval_changed)
        self.start_button = button("开始监视", self.toggle, "primary", "play")
        self.start_button.setFixedWidth(control_width)
        # Header: title on the left, run controls on the right.
        header = row(label("寄存器监视", "title"), self.component_label,
                     label("采样间隔", "muted"), self.interval_spin, self.start_button, spacing=8)
        header.setStretchFactor(self.component_label, 1)
        header.insertSpacing(1, 4)
        header.insertSpacing(3, 16)
        layout.addLayout(header)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(9)
        self.splitter.setChildrenCollapsible(False)
        side = QWidget()
        side.setMinimumWidth(220)
        side.setMaximumWidth(self.width() // 2)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称 / 偏移")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("search", size=16), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self._filter_list)
        self.list = QListWidget()
        self.list.setObjectName("monitorList")
        self.list.setItemDelegate(_MonitorRegisterDelegate(self.list))
        self.list.setSelectionMode(QListWidget.NoSelection)   # the delegate marks the focused row
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.itemChanged.connect(self._item_changed)
        self.list.currentItemChanged.connect(lambda item, _: item is not None and self.focus_register(item.data(Qt.UserRole)))
        self.list.viewport().installEventFilter(self)   # rows are disabled while running but still pick the lane
        self.count_label = label("", "muted")
        self.clear_selection_button = button("取消选择", self.clear_selection, "flat")
        self.clear_selection_button.setToolTip("取消所有寄存器的勾选，包括搜索隐藏的条目")
        side_layout.addWidget(self.search)
        side_layout.addWidget(self.list, 1)
        side_layout.addLayout(row(self.count_label, 1, self.clear_selection_button, spacing=8))
        self.splitter.addWidget(side)
        main_panel = QWidget()
        main = QVBoxLayout(main_panel)
        main.setVerticalSizeConstraint(QLayout.SetMinimumSize)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(8)
        self.signed_check = QCheckBox("有符号")
        self.signed_check.setChecked(self.settings["signed"])
        self.signed_check.setToolTip("按 32 位有符号数绘制与显示（如位置、计数差值）")
        self.signed_check.toggled.connect(self._signed_changed)
        self.clear_button = button("清空数据", self.clear, "flat", "clear")
        self.clear_button.setToolTip("清空当前采样数据与标记")
        self.export_button = button("导出 CSV", self.export_csv, "flat", "export")
        for control in (self.clear_button, self.export_button):
            control.setFixedWidth(control_width)
        self.plot = MonitorPlot()
        self.plot.signed = self.settings["signed"]
        self.zoom_label = label("100%", "muted")
        self.zoom_label.setFixedWidth(58)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_button = button("Zoom", lambda: self.plot.set_zoom(1.0), "flat", "axis")
        self.zoom_button.setToolTip("查看全局图表：恢复 100% 比例，显示全部保留的数据。\nCtrl + 滚轮以鼠标位置为中心缩放。")
        self.zoom_button.setAccessibleName("Zoom：查看全局图表")
        self.plot.zoom_changed.connect(self._zoom_changed)
        self.marker_label = label("A: —   B: —   Δt: —", "muted")
        self.marker_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.marker_label.setWordWrap(True)
        self.marker_label.setMinimumHeight(32)
        self.marker_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.marker_label.setStyleSheet("QLabel { background: #F7F9FC; border: 1px solid #DEE5EC; "
                                       "border-radius: 5px; padding: 4px 10px; }")
        self.clear_markers_button = button("清除标记", self.plot.clear_markers, "flat")
        self.clear_markers_button.setToolTip("清除 A/B 标记，保留采样数据")
        for control in (self.clear_selection_button, self.clear_button, self.export_button,
                        self.zoom_button, self.clear_markers_button):
            control.setProperty("outlined", True)
        self.clear_markers_button.setEnabled(False)
        self.plot.markers_changed.connect(self._markers_changed)
        self.plot.lane_clicked.connect(self._lane_clicked)
        # Toolbar: view options on the left, data actions on the right; markers sit under the chart.
        main.addLayout(row(self.zoom_button, self.zoom_label, self.signed_check, 1,
                           self.clear_button, self.export_button, spacing=8))
        main.addWidget(self.plot, 1)
        markers = row(self.marker_label, self.clear_markers_button, spacing=8)
        markers.setStretchFactor(self.marker_label, 1)
        main.addLayout(markers)
        self.splitter.addWidget(main_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([270, 800])
        self.splitter.handle(1).setToolTip("左右拖动调整参数选择栏宽度")
        layout.addWidget(self.splitter, 1)
        self.footer = QFrame()   # status bar
        self.footer.setObjectName("monitorFooter")
        self.footer.setStyleSheet("QFrame#monitorFooter { background: white; border: 1px solid #DEE5EC; "
                                 "border-radius: 7px; }")
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(12, 6, 12, 6)
        footer_layout.setSpacing(10)
        self.state_dot = QLabel()
        self.state_dot.setFixedSize(10, 10)
        self.status = ElidedLabel("", "muted")
        self.status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.status.setWordWrap(False)
        self.status.setFixedHeight(20)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        footer_layout.addWidget(self.state_dot, 0, Qt.AlignVCenter)
        footer_layout.addWidget(self.status, 1)
        layout.addWidget(self.footer)
        self._status_kind = ""
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(50)
        self.refresh_timer.timeout.connect(self._refresh)
        self._update_count()
        self._set_status("勾选寄存器后点「开始监视」。")
        self.search.setFocus()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "splitter"):
            side = self.splitter.widget(0)
            if side is not None:
                side.setMaximumWidth(max(side.minimumWidth(), self.width() // 2))

    # -- register list -------------------------------------------------------------------
    def set_registers(self, title, registers, preselect=(), key=None):
        """List a component's registers; ignored while a run is active (its list stays)."""
        if self.state != "idle":
            return False
        key = title if key is None else key
        addresses = {reg["_address"] for reg in registers}
        saved = self.settings["selections"].get(key)
        chosen = (set(saved) if saved is not None else set(preselect)) & addresses
        self._selection_key = key
        self.component_label.setText(title)
        self.list.blockSignals(True)
        self.list.clear()
        for reg in registers:
            mode = " · 只写" if reg.get("write_only") else ""
            readable = not any(reg.get(flag) for flag in ("write_only", "read_clear", "read_to_clear", "read_side_effect"))
            if not readable and not mode:
                mode = " · 读取有副作用"
            item = QListWidgetItem(f'{reg["name"]}   +0x{parse_addr(reg["offset"]):03X}{mode}')
            item.setData(Qt.UserRole, reg["_address"])
            item.setData(Qt.UserRole + 1, reg["name"])
            item.setData(Qt.UserRole + 2, parse_addr(reg["offset"]))
            item.setToolTip(f'{reg["name"]}\n0x{reg["_address"]:08X}')
            item.setFlags((Qt.ItemIsEnabled | Qt.ItemIsUserCheckable) if readable else Qt.NoItemFlags)
            if not readable:
                item.setToolTip(item.toolTip() + "\n不支持持续监视此寄存器。")
            item.setCheckState(Qt.Checked if readable and reg["_address"] in chosen else Qt.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._trim_checks()
        self._filter_list()
        self._update_count()
        self._remember_selection()
        return True

    def _items(self):
        return [self.list.item(i) for i in range(self.list.count())]

    def checked_registers(self):
        return [(item.data(Qt.UserRole + 1), item.data(Qt.UserRole)) for item in self._items()
                if item.checkState() == Qt.Checked and item.flags() & Qt.ItemIsUserCheckable]

    def checked_addresses(self):
        return [address for _, address in self.checked_registers()]

    def _trim_checks(self):
        self.list.blockSignals(True)
        for item in [item for item in self._items() if item.checkState() == Qt.Checked][MONITOR_MAX_REGISTERS:]:
            item.setCheckState(Qt.Unchecked)
        self.list.blockSignals(False)

    def _item_changed(self, item):
        if item.checkState() == Qt.Checked and len(self.checked_registers()) > MONITOR_MAX_REGISTERS:
            self.list.blockSignals(True)
            item.setCheckState(Qt.Unchecked)
            self.list.blockSignals(False)
            self._set_status(f"最多同时监视 {MONITOR_MAX_REGISTERS} 个寄存器。", "warn")
        self._update_count()
        self._remember_selection()

    def _remember_selection(self):
        if self._selection_key is not None:
            self.settings["selections"][self._selection_key] = self.checked_addresses()
            self.settings_changed.emit(copy.deepcopy(self.settings))

    def eventFilter(self, watched, event):
        if (watched is self.list.viewport() and event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton):
            item = self.list.itemAt(event.position().toPoint())
            if item is not None:
                self.focus_register(item.data(Qt.UserRole))
        return super().eventFilter(watched, event)

    def focus_register(self, address):
        """Mark one register in the list and highlight its lane when it is plotted."""
        self.list.itemDelegate().focus = address
        self.list.viewport().update()
        self._sync_highlight()

    def _sync_highlight(self):
        address = self.list.itemDelegate().focus
        addresses = [monitored for _, monitored in self.monitored]
        self.plot.set_highlight(addresses.index(address) if address in addresses else None)

    def _lane_clicked(self, column):
        if column >= len(self.monitored):
            return
        address = self.monitored[column][1]
        item = next((item for item in self._items() if item.data(Qt.UserRole) == address), None)
        if item is not None and not item.isHidden():
            self.list.scrollToItem(item)
        self.focus_register(address)

    def _filter_list(self, *_):
        needle = self.search.text().strip().lower()
        for item in self._items():
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _update_count(self):
        count = len(self.checked_registers())
        self.count_label.setText(f"已选 {count} / {MONITOR_MAX_REGISTERS}")
        self.clear_selection_button.setEnabled(self.state == "idle" and count > 0)

    def clear_selection(self):
        if self.state != "idle":
            return False
        with QSignalBlocker(self.list):
            for item in self._items():
                if item.checkState() == Qt.Checked:
                    item.setCheckState(Qt.Unchecked)
        self._update_count()
        self._remember_selection()
        return True

    # -- run control ---------------------------------------------------------------------
    def toggle(self):
        if self.state == "idle":
            self.start()
        else:
            self.stop()

    def start(self):
        if self.state != "idle":
            return False
        chosen = self.checked_registers()
        if not chosen:
            self._set_status("请先在左侧勾选要监视的寄存器。", "warn")
            return False
        session = self._session_provider()
        if session is None:
            self._set_status("请先连接 SSH，再开始监视。", "warn")
            return False
        interval = self.interval_spin.value()
        self._epoch += 1
        token = self._epoch
        self._stop = stop = threading.Event()
        self.monitored = chosen
        offsets = {item.data(Qt.UserRole): item.data(Qt.UserRole + 2) for item in self._items()}
        self.monitored_offsets = [offsets[address] for _, address in chosen]
        self.sampler = ""
        self._last_data = None
        self._shown_pause = 0
        self.interval_ms = interval
        self.buffer = MonitorBuffer(len(chosen))
        self.plot.set_series(self.buffer, [name for name, _ in chosen], self.monitored_offsets)
        self._sync_highlight()
        addresses = [address for _, address in chosen]
        bridge = self.bridge
        worker = Worker(lambda progress: session.start_monitor(
            addresses, interval, lambda samples: bridge.samples.emit(token, samples),
            lambda message: bridge.stopped.emit(token, message), stop,
            on_sampler=lambda kind: bridge.sampler.emit(token, kind)))
        worker.signals.result.connect(lambda ok: bridge.started.emit(token, bool(ok)))
        worker.signals.failed.connect(lambda message: bridge.stopped.emit(token, "启动监视失败：" + message))
        worker.signals.finished.connect(lambda: self._workers.discard(worker))
        self._workers.add(worker)
        self._set_state("starting")
        self._set_status(f"正在启动板端采样（{len(chosen)} 个寄存器，间隔 {interval} ms）…")
        self.pool.start(worker)
        return True

    def stop(self):
        if self.state == "idle":
            return
        self._stop.set()
        self._epoch += 1   # queued samples from this run must not refill a cleared buffer
        self._set_state("idle")
        self._refresh()
        self._set_status(self._summary("已停止"))

    def clear(self):
        if self.monitored:
            self.buffer = MonitorBuffer(len(self.monitored))
            self.plot.set_series(self.buffer, [name for name, _ in self.monitored], self.monitored_offsets)
            self._sync_highlight()
        self._set_status("已清空。" if self.state == "idle" else self._summary("监视中"))

    def _started(self, token, ok):
        if token != self._epoch or self.state != "starting":
            return
        if ok:
            self._set_state("running")
            self._set_status(self._summary("监视中"))
        else:
            self._set_state("idle")

    def _samples(self, token, samples):
        if token == self._epoch and self.buffer is not None:
            self.buffer.extend(samples)
            self._last_data = time.monotonic()
            self._dirty = True

    def _sampler_known(self, token, kind):
        if token == self._epoch:
            self.sampler = kind
            self._dirty = True

    def _stopped(self, token, message):
        if token != self._epoch:
            return
        if self.state != "idle":
            self._set_state("idle")
        self._refresh()
        if message:
            self._set_status(message, "error")
        else:
            self._set_status(self._summary("已停止"))

    def _set_state(self, state):
        self.state = state
        idle = state == "idle"
        self.start_button.setText("开始监视" if idle else "停止")
        self.start_button.setObjectName("primary" if idle else "danger")
        self.start_button.setIcon(icon("play", "#FFFFFF") if idle else icon("stop", "#B64242"))
        restyle(self.start_button)
        # Lock the rows while keeping the view and its scrollbar interactive.
        with QSignalBlocker(self.list):
            for item in self._items():
                flags = item.flags()
                if flags & Qt.ItemIsUserCheckable:
                    item.setFlags(flags | Qt.ItemIsEnabled if idle else flags & ~Qt.ItemIsEnabled)
        for control in (self.search, self.interval_spin):
            control.setEnabled(idle)
        self._update_count()
        self._update_state_dot()
        if idle:
            self.refresh_timer.stop()
        else:
            self.refresh_timer.start()

    def _summary(self, prefix):
        buffer = self.buffer
        if buffer is None or not len(buffer):
            return f"{prefix} · 尚无数据"
        parts = [prefix, f"已采 {buffer.total:,} 点", f"用时 {buffer.times[-1]:.1f} s"]
        details = []
        period = buffer.period()
        if period:
            details.append(f"实际 {period * 1000:.2f} ms/点（设定 {self.interval_ms} ms）")
            if self._lagging():
                details.append("板端跟不上设定间隔")
        if buffer.failed:
            details.append(f"读取失败 {buffer.failed} 次（红色刻度）")
        if buffer.dropped:
            details.append(f"保留 {len(buffer):,} 点 · 已丢弃 {buffer.dropped:,} 点旧数据，CSV 仅导出保留部分")
        if self.sampler in SAMPLER_LABELS:
            details.append(SAMPLER_LABELS[self.sampler])
        return " · ".join(parts + details)

    def _lagging(self):
        period = self.buffer.period() if self.buffer is not None else None
        return bool(period) and period * 1000 > self.interval_ms * 1.5

    def _paused_seconds(self):
        """Whole seconds without new samples once a run has gone quiet (network stall), else 0."""
        if self.state != "running" or self._last_data is None:
            return 0
        idle = time.monotonic() - self._last_data
        return int(idle) if idle > max(MONITOR_STALE_TIMEOUT_S, self.interval_ms / 1000 * 3 + 0.5) else 0

    def _refresh(self):
        paused = self._paused_seconds()
        if self._dirty or paused != self._shown_pause:
            self._shown_pause = paused
            if self._dirty:
                self._dirty = False
                self.plot.update()
            if self.state != "running":
                return
            if paused:
                self._set_status(self._summary(f"数据暂停 {paused} s（网络或板端无响应，恢复后自动继续）"), "warn")
            else:
                self._set_status(self._summary("监视中"),
                                 "warn" if self.buffer.failed or self.buffer.dropped or self._lagging() else "")

    def _set_status(self, text, kind=""):
        self.status.setText(" · ".join(text.splitlines()))
        self.status.setStyleSheet({"warn": "color: #9A661D;", "error": "color: #B14040;"}.get(kind, ""))
        self._status_kind = kind
        self._update_state_dot()

    def _update_state_dot(self):
        color = STATUS_COLORS.get(self._status_kind) or STATE_COLORS.get(self.state, STATE_COLORS["idle"])
        self.state_dot.setStyleSheet(f"QLabel {{ background: {color}; border-radius: 5px; }}")

    def _zoom_changed(self, factor):
        self.zoom_label.setText(f"{factor * 100:.0f}%")

    def _markers_changed(self, markers):
        first, second = markers
        def stamp_text(stamp):
            return "—" if stamp is None else f"{stamp:.6f} s"
        difference = abs(second - first) if first is not None and second is not None else None
        self.marker_label.setText(f"A: {stamp_text(first)}   B: {stamp_text(second)}   Δt: {stamp_text(difference)}")
        self.clear_markers_button.setEnabled(any(stamp is not None for stamp in markers))

    def _signed_changed(self, on):
        self.plot.signed = on
        self.settings["signed"] = on
        self.settings_changed.emit(copy.deepcopy(self.settings))
        self.plot.update()

    def export_csv(self):
        buffer = self.buffer
        if buffer is None or not len(buffer):
            self._set_status("还没有可导出的数据。", "warn")
            return None
        path, _ = QFileDialog.getSaveFileName(self, "导出监视数据", f"monitor-{datetime.now():%Y%m%d-%H%M%S}.csv",
                                              "CSV 文件 (*.csv)")
        if not path:
            return None
        signed = self.signed_check.isChecked()
        failures = [set(column) for column in buffer.failures]
        columns = [buffer.column(index, signed) for index in range(len(self.monitored))]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["time_s"] + [f"{name} (0x{address:08X})" for name, address in self.monitored])
                for index, stamp in enumerate(buffer.times):
                    writer.writerow([f"{stamp:.6f}"] + ["" if index in failures[column] else f"{values[index]:.0f}"
                                                        for column, values in enumerate(columns)])
        except OSError as exc:
            self._set_status(f"导出失败：{exc}", "error")
            return None
        note = f"（已丢弃 {buffer.dropped:,} 点旧数据）" if buffer.dropped else ""
        self._set_status(f"已导出保留的 {len(buffer):,} 点到 {path}{note}", "warn" if buffer.dropped else "")
        return path

    def shutdown(self):
        """Owner is closing: stop sampling and drop late results."""
        self._epoch += 1
        self._stop.set()
        self._set_state("idle")

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)

    def reject(self):
        self.stop()
        super().reject()
