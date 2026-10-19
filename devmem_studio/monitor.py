# -*- coding: utf-8 -*-
"""Register monitor: a few registers sampled on the board and plotted against time."""
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
import copy
from itertools import chain
import csv
from datetime import datetime
import math
import threading
import time

from PySide6.QtCore import Qt, QEvent, QObject, QPoint, QPointF, QRectF, QSignalBlocker, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QPolygonF, QTransform
from PySide6.QtWidgets import (QDialog, QWidget, QFrame, QLabel, QLayout, QHBoxLayout, QVBoxLayout, QSplitter,
                               QListWidget, QListWidgetItem,
                               QLineEdit, QCheckBox, QSpinBox, QFileDialog, QSizePolicy,
                               QScrollArea, QStyledItemDelegate, QStyle)

from .core import (MONITOR_INTERVAL_MS, MONITOR_MAX_INTERVAL_MS, MONITOR_MAX_REGISTERS, MONITOR_MIN_INTERVAL_MS,
                   MONITOR_STALE_TIMEOUT_S, normalize_monitor_settings, parse_addr)
from .csv_export import EXPORT_FILTERS, csv_export, export_path
from .theme import icon
from .widgets import ElidedLabel, label, button, row, restyle, Worker
from .window_state import ManagedDialog

SERIES_COLORS = ("#2463DC", "#168578", "#B57518", "#C44848", "#7A4FC4", "#1F8FB3", "#6E7F1F", "#C2477F")
SAMPLE_LIMIT = 200_000   # per run; the oldest quarter is dropped beyond it
MIN_SAMPLE_LIMIT = 50_000
VALUE_MEMORY_BUDGET = 16 * 1024 * 1024   # raw values per run; signed values are lazy
RANGE_BLOCK = 32
SAMPLER_LABELS = {"shell": "devmem 循环（板端 CPU 占用较高）", "demo": "演示数据"}
STATE_COLORS = {"idle": "#A3B1BF", "starting": "#2463DC", "running": "#2E9D5B", "paused": "#D59A2E"}
PAUSE_FILL = QColor(213, 154, 46, 28)   # chart band where sampling was paused
STATUS_COLORS = {"warn": "#D59A2E", "error": "#C84B4B"}


def to_signed(value):
    return value - (1 << 32) if value >= 0x80000000 else value


def monitor_sample_limit(count):
    """Keep the raw monitor history within a predictable memory budget.

    A one-register run keeps the full history. Wider runs use fewer points so the
    raw value arrays stay near the same budget; the UI reports the resulting
    retention through ``MonitorBuffer.limit``.
    """
    count = max(1, int(count))
    return max(MIN_SAMPLE_LIMIT, min(SAMPLE_LIMIT, VALUE_MEMORY_BUDGET // (4 * count)))


class MonitorBuffer:
    """Shared time axis plus raw/signed value columns. A failed read holds the previous
    value and is indexed separately, so min()/max() run on plain C arrays."""

    def __init__(self, count, limit=SAMPLE_LIMIT, addresses=(), store_signed=True):
        addresses = tuple(addresses)
        if addresses and len(addresses) != count:
            raise ValueError("monitor buffer address count must match column count")
        self.addresses = addresses
        self.limit = limit
        self.t0 = None
        self.times = array("d")
        self.raw = [array("I") for _ in range(count)]
        # Signed values are only needed when the signed plot is first selected.
        # Keeping this lazy saves one 32-bit array per register for the common
        # unsigned view, while preserving the old eager behavior for callers
        # that inspect ``signed`` directly (including compatibility tests).
        self._store_signed = bool(store_signed)
        self.signed = [array("i") for _ in range(count)] if self._store_signed else [None] * count
        self.failures = [array("I") for _ in range(count)]   # retained sample indices
        self.total = 0
        self.failed = 0
        self.revision = 0
        self._ranges = [([], [], [], []) for _ in range(count)]
        self._range_offset = 0   # retained data's position in the first cached block
        self.pauses = []         # (begin, end) seconds without sampling between runs
        self._resumed = False
        self._levels = {}        # (column, signed) -> [first block, mins, maxs] per 2**k-sample level

    def __len__(self):
        return len(self.times)

    def mark_resume(self):
        """The next sample starts a resumed run: record the gap before it."""
        self._resumed = bool(self.times)

    def extend(self, samples):
        for stamp, values in samples:
            if self.t0 is None:
                self.t0 = stamp
            index = len(self.times)
            moment = stamp - self.t0
            if self._resumed:
                self._resumed = False
                previous = self.times[-1]
                if moment < previous:   # board clock restarted (reboot): continue the axis
                    self.t0 -= previous - moment
                    moment = previous
                elif moment > previous:
                    self.pauses.append((previous, moment))
            self.times.append(moment)
            for column, value in enumerate(values):
                raw = self.raw[column]
                if value is None:
                    self.failures[column].append(index)
                    self.failed += 1
                    value = int(raw[-1]) if raw else 0
                raw.append(value)
                signed = to_signed(value)
                if self.signed[column] is not None:
                    self.signed[column].append(signed)
                for summary, candidate, minimum in zip(self._ranges[column], (value, value, signed, signed),
                                                        (True, False, True, False)):
                    if (index + self._range_offset) % RANGE_BLOCK == 0:
                        summary.append(candidate)
                    elif (minimum and candidate < summary[-1]) or (not minimum and candidate > summary[-1]):
                        summary[-1] = candidate
        self.total += len(samples)
        self.revision += 1
        if len(self.times) > self.limit:
            self._drop(len(self.times) - self.limit * 3 // 4)

    def _drop(self, count):
        blocks, self._range_offset = divmod(self._range_offset + count, RANGE_BLOCK)
        del self.times[:count]
        self.pauses = [gap for gap in self.pauses if gap[1] > self.times[0]]
        for column in range(len(self.raw)):
            del self.raw[column][:count]
            if self.signed[column] is not None:
                del self.signed[column][:count]
            failures = self.failures[column]
            kept = failures[bisect_left(failures, count):]
            self.failures[column] = array("I", (i - count for i in kept))
            # The failure counter must track the retained arrays: a failure whose
            # index fell inside the dropped window is gone from `failures` and must
            # not keep inflating `self.failed` (the chart red ticks and the export
            # both derive from the arrays, and _summary reports the counter).
            self.failed -= len(failures) - len(kept)
            # Whole blocks remain valid. The partial leading block is scanned
            # directly by extrema(), so discarded values cannot affect its result.
            for summary in self._ranges[column]:
                del summary[:blocks]
        dropped = self.dropped
        for pyramid in self._levels.values():
            for level, entry in enumerate(pyramid, 1):
                first = -(-dropped >> level)   # blocks holding dropped samples go
                del entry[1][:first - entry[0]], entry[2][:first - entry[0]]
                entry[0] = first

    def column(self, index, signed=False):
        if not signed:
            return self.raw[index]
        values = self.signed[index]
        if values is None:
            values = self.signed[index] = array("i", (to_signed(value) for value in self.raw[index]))
        return values

    def blocks(self, column, signed, level):
        """(first absolute block, mins, maxs) of whole 2**level-sample blocks, built lazily;
        absolute sample index = retained index + dropped."""
        values = self.column(column, signed)
        pyramid = self._levels.setdefault((column, signed), [])
        first, mins, maxs = self.dropped, values, values
        for depth in range(level):
            if depth == len(pyramid):
                pyramid.append([-(-first // 2), array(values.typecode), array(values.typecode)])
            entry = pyramid[depth]
            begin, stop = entry[0] + len(entry[1]), (first + len(mins)) // 2
            if stop > begin:
                a, b = 2 * begin - first, 2 * stop - first
                entry[1].extend(map(min, mins[a:b:2], mins[a + 1:b:2]))
                entry[2].extend(map(max, maxs[a:b:2], maxs[a + 1:b:2]))
            first, mins, maxs = entry
        return first, mins, maxs

    def extrema(self, column, begin, end, signed=False):
        """Exact extrema: inspect small boundary fragments and cached whole blocks."""
        values = self.column(column, signed)
        first = (begin + self._range_offset + RANGE_BLOCK - 1) // RANGE_BLOCK
        last = (end + self._range_offset) // RANGE_BLOCK
        if first >= last:
            chunk = values[begin:end]
            return min(chunk), max(chunk)
        offset = 2 if signed else 0
        low = min(self._ranges[column][offset][first:last])
        high = max(self._ranges[column][offset + 1][first:last])
        for chunk in (values[begin:first * RANGE_BLOCK - self._range_offset],
                      values[last * RANGE_BLOCK - self._range_offset:end]):
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
    LABEL_WIDTH = 260
    LANE_MIN_HEIGHT = 88
    AXIS_HEIGHT = 26
    MIN_ZOOM = 0.25
    ZOOM_STEP = 1.25
    ZOOM_EASE = 0.35   # share of the remaining (log) zoom covered per glide frame

    def __init__(self, parent=None):
        super().__init__(parent)
        self.buffer = None
        self.names = []
        self.offsets = []
        self.columns = []   # buffer column of each lane; None = no data
        self.zoom_factor = 1.0
        self.sample_interval_s = MONITOR_INTERVAL_MS / 1000
        self._view_range = None
        self.signed = False
        self.highlight = None   # lane of the register picked in the list
        self._hover = None
        self.markers = [None, None]
        self._next_marker = 0
        self._drag_marker = None
        self.export_range = None
        self._range_press = None
        self._range_dragging = False
        self._range_resize = None
        self._pan_press_pos = None
        self._pan_start_range = None
        self._pan_dragging = False
        self._pan_live_active = False
        self._pan_release_pending = False
        self._pan_render_timer = QTimer(self)
        self._pan_render_timer.setSingleShot(True)
        self._pan_render_timer.timeout.connect(self._flush_pan_render)
        self._pan_painted = 0.   # perf_counter of the last pan frame
        self._piece_cache = OrderedDict()
        self._zoom_target = None   # Ctrl+wheel glide: wheel events move the target, frames ease toward it
        self._zoom_anchor = None
        self._zoom_origin = None   # (view range, zoom) when the glide began
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setInterval(16)
        self._zoom_timer.timeout.connect(self._zoom_tick)
        self._trace_cache = {}
        self._cache_key = None
        self._scene_cache = None
        self._scene_key = None
        self._scene_range = None
        self._scene_transform = None
        self._scene_revision = None
        self._scene_rect = None
        self._frame_count = 0
        self._lane_scenes = OrderedDict()
        self._frame_created = 0.
        self._frame_render_ms = 0.
        self._scrolling = False
        self._scroll_idle = QTimer(self)
        self._scroll_idle.setSingleShot(True)
        self._scroll_idle.setInterval(120)
        self._scroll_idle.timeout.connect(self._scroll_finished)
        self._prepared_frame = None
        self._prepare_timer = QTimer(self)
        self._prepare_timer.setSingleShot(True)
        self._prepare_timer.timeout.connect(self._prepare_one_lane)
        self._pending_tiles = OrderedDict()
        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.setInterval(1)   # yield to input between individual lane renders
        self._tile_timer.timeout.connect(self._prepare_tile)
        self._mouse_repaint = False
        self._data_refresh_requested = False
        self._hold_display = False   # keep the drawn time axis for the click that placed a marker
        self._drag_moved = False
        self._toggle_marker = None
        self._toggle_stamp = None
        self._marker_press_pos = None
        self._axis_sync_pending = False
        self.axis_view = None   # pinned footer; when set, the axis is not drawn inside the scrolling chart
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setMinimumSize(420, 220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAccessibleDescription("单击图表放置 A/B 标记，同一位置再次单击取消该标记，拖动标记调整位置，右键清除标记。"
                                      "左键拖选导出时间段，拖动选区左右边缘调整范围，在选区外拖动重新选择，右键清除选区。"
                                      "单击左侧寄存器名称高亮曲线。滚轮上下浏览，Ctrl + 滚轮缩放时间轴，按住中键左右拖动平移时间轴。")

    def set_series(self, buffer, names, offsets=(), columns=None):
        self.clear_markers()
        self.export_range = None
        self._range_press = None
        self._range_dragging = False
        self._range_resize = None
        if self._pan_press_pos is not None:
            self.unsetCursor()   # drop the grab cursor of an interrupted pan
        self._pan_press_pos = None
        self._pan_start_range = None
        self._pan_dragging = False
        self._pan_live_active = False
        self._pan_release_pending = False
        self._pan_render_timer.stop()
        self._stop_zoom_glide()
        if buffer is not self.buffer:
            self._piece_cache.clear()
        self.buffer, self.names = buffer, list(names)
        self.highlight = None
        self._hover = None
        self._view_range = None
        self.offsets = list(offsets) if offsets else [None] * len(self.names)
        self.columns = list(columns) if columns is not None else list(range(len(self.names)))
        self._scroll_idle.stop()
        self._scrolling = False
        self._hold_display = False
        self._drag_moved = False
        self._relayout()

    def set_lanes(self, buffer, names, offsets, columns):
        """Lanes follow a new pick; markers and zoom stay while the data does."""
        names, offsets, columns = list(names), list(offsets), list(columns)
        if buffer is not self.buffer:
            self.set_series(buffer, names, offsets, columns)
        elif (names, offsets, columns) != (self.names, self.offsets, self.columns):
            self.names, self.offsets, self.columns = names, offsets, columns
            self.highlight = None
            self._relayout()

    def _relayout(self):
        self._tile_timer.stop()
        self._pending_tiles.clear()
        self._scene_cache = None
        self._scene_key = None
        self._lane_scenes.clear()
        self._trace_cache.clear()
        self._cache_key = None
        self._prepare_timer.stop()
        self._prepared_frame = None
        self.setMinimumHeight(max(220, len(self.names) * self.LANE_MIN_HEIGHT + max(0, len(self.names) - 1) * 8
                                  + self._axis_reserve() + 6))
        self._request_paint()

    def time_range(self):
        if self._view_range is not None:
            return self._view_range
        times = self.buffer.times if self.buffer else array("d")
        end = times[-1] if times else 0.0
        earliest = times[0] if times else 0.0
        span = max(self.minimum_time_span(), max(end - earliest, 1.0) / self.zoom_factor)
        begin = max(earliest, end - span)
        return begin, max(end, begin + span)

    def minimum_time_span(self):
        return 2 * self.sample_interval_s

    def maximum_zoom(self):
        # An anchored view may stay parked while new samples extend the buffer.
        # Its scale is based on the displayed window, not the growing history.
        if self._view_range is not None:
            base_span = (self._view_range[1] - self._view_range[0]) * self.zoom_factor
        else:
            times = self.buffer.times if self.buffer else ()
            base_span = max(times[-1] - times[0], 1.0) if times else 1.0
        return max(1.0, base_span / self.minimum_time_span())

    def set_zoom(self, factor, anchor_x=None):
        if anchor_x is None:
            self._stop_zoom_glide()   # Zoom button reset wins over a glide in progress
            self._view_range = None
        factor = max(self.MIN_ZOOM, min(self.maximum_zoom(), factor))
        if anchor_x is None:
            self._view_range = None
        else:
            if factor == self.zoom_factor and self._view_range is not None:
                return
            begin, end = self.time_range()
            lane = self.lanes()[0]
            fraction = max(0.0, min(1.0, (anchor_x - lane.left()) / lane.width()))
            anchor = begin + fraction * (end - begin)
            span = max(self.minimum_time_span(), (end - begin) * self.zoom_factor / factor)
            begin = anchor - fraction * span
            self._view_range = (begin, begin + span)
        if factor != self.zoom_factor:
            self.zoom_factor = factor
            self.zoom_changed.emit(factor)
        self._request_paint()

    def zoom(self, steps, anchor_x=None):
        self.set_zoom(self.zoom_factor * self.ZOOM_STEP ** max(-32, min(32, steps)), anchor_x)

    def zoom_smoothly(self, steps, anchor_x):
        """Glide to the new zoom over a few frames; wheel events during a glide only
        move its target, so a fast spin never queues one full redraw per notch."""
        base = self._zoom_target if self._zoom_target is not None else self.zoom_factor
        target = base * self.ZOOM_STEP ** max(-32, min(32, steps))
        self._zoom_target = max(self.MIN_ZOOM, min(self.maximum_zoom(), target))
        if self._zoom_origin is None or anchor_x != self._zoom_anchor:
            # Frames are computed from the glide's start, so the last one equals a single jump.
            self._zoom_origin = (self._view_range, self.zoom_factor)
        self._zoom_anchor = anchor_x
        if not self._zoom_timer.isActive():
            self._zoom_timer.start()
            self._zoom_tick()   # the first frame follows the wheel at once

    def _zoom_tick(self):
        target = self._zoom_target
        if target is None:
            self._zoom_timer.stop()
            return
        ratio = target / self.zoom_factor
        factor = target if abs(math.log(ratio)) < 0.005 else self.zoom_factor * ratio ** self.ZOOM_EASE
        self._zoom_to(factor)
        if self.zoom_factor != factor or factor == target:   # reached, or held at a zoom limit
            self._stop_zoom_glide()

    def _zoom_to(self, factor):
        self._view_range, self.zoom_factor = self._zoom_origin
        self.set_zoom(factor, self._zoom_anchor)
        self._request_paint()   # set_zoom skips it when the factor equals the origin's

    def finish_zoom(self):
        """Jump to the end of a running glide."""
        if self._zoom_target is not None:
            target = self._zoom_target
            self._zoom_to(target)
            self._stop_zoom_glide()

    def _stop_zoom_glide(self):
        self._zoom_target = None
        self._zoom_origin = None
        self._zoom_timer.stop()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            steps = event.angleDelta().y() / 120 or event.pixelDelta().y() / 60
            lane = self.lanes()[0]
            if steps and lane.left() <= event.position().x() <= lane.right():
                self._hover = event.position().x()
                self.zoom_smoothly(steps, event.position().x())
                event.accept()
                return
        super().wheelEvent(event)

    def _axis_reserve(self):
        return 0 if self.axis_view is not None else self.AXIS_HEIGHT

    def lanes(self):
        count = max(1, len(self.names))
        top, bottom, gap = 6.0, self.height() - self._axis_reserve(), 8.0
        height = max(8.0, (bottom - top - gap * (count - 1)) / count)
        left, right = float(self.LABEL_WIDTH), self.width() - 12.0
        return [QRectF(left, top + i * (height + gap), max(10.0, right - left), height) for i in range(count)]

    def axis_band(self):
        """Time axis pinned to the bottom of the visible chart, not the last lane."""
        visible = self._visible_rect()
        top = max(visible.top(), visible.bottom() - self.AXIS_HEIGHT)
        return QRectF(visible.left(), top, visible.width(), max(0.0, visible.bottom() - top))

    def _on_axis(self, position):
        if self.axis_view is not None:
            return False
        band = self.axis_band()
        return band.top() <= position.y() <= band.bottom() and band.left() <= position.x() <= band.right()

    def _time_ticks(self, start, end):
        span = max(end - start, 1e-9)
        step = nice_step(span / max(1.0, self.lanes()[0].width() / 110))
        ticks = [step * k for k in range(math.ceil(start / step), math.floor(end / step) + 1)]
        return step, ticks

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
        column = self.columns[column]
        if column is None:
            return "—"
        failures = self.buffer.failures[column]
        at = bisect_left(failures, index)
        if at < len(failures) and failures[at] == index:
            return "读取失败"
        raw = int(self.buffer.raw[column][index])
        return f"0x{raw:X} · {to_signed(raw) if self.signed else raw}"

    def _index_at(self, stamp, count=None):
        """Read the held step value; a time outside retained data has no value."""
        if stamp is None or self.buffer is None or not len(self.buffer):
            return None
        times = self.buffer.times
        count = len(times) if count is None else min(count, len(times))
        if not count or not times[0] <= stamp <= times[count - 1]:
            return None
        return bisect_right(times, stamp, hi=count) - 1

    def _display_sample_count(self):
        if self._scene_range is not None and self._scene_transform == self._transform_key():
            return self._frame_count
        return len(self.buffer) if self.buffer is not None else 0

    def _hover_index(self):
        lane = self.lanes()[0]
        if self._hover is None or not lane.left() <= self._hover <= lane.right():
            return None
        start, end = self._display_time_range()
        return self._index_at(start + (self._hover - lane.left()) / lane.width() * (end - start),
                              self._display_sample_count())

    def header_values(self, column, indices=None):
        if indices is None:
            indices = (self._hover_index(), *(self._index_at(stamp) for stamp in self.markers))
        return tuple(self.value_text(column, index) if index is not None else "—" for index in indices)

    def readout_values(self, column, indices=None):
        latest = self.value_text(column, len(self.buffer) - 1) if self.buffer is not None and len(self.buffer) else "—"
        return (latest, *self.header_values(column, indices))

    def _transform_key(self):
        first = self.buffer.times[0] if self.buffer is not None and len(self.buffer) else None
        return (id(self.buffer), self.zoom_factor, self._view_range, self.width(), self.height(), first)

    def _display_time_range(self):
        if self._scene_range is not None and self._scene_transform == self._transform_key():
            return self._scene_range
        return self.time_range()

    def refresh_data(self):
        """A data frame takes priority over pointer-only repaint requests."""
        self._data_refresh_requested = True
        self._request_paint()

    def _request_paint(self):
        """Repaint the viewport only. The chart widget is much taller than the screen."""
        area = self._visible_rect().toAlignedRect()
        self.update(area if area.isValid() and not area.isEmpty() else self.rect())
        if self.axis_view is not None:
            self.axis_view.update()

    def _begin_scroll(self):
        self._scrolling = True
        self._scroll_idle.start()

    def scroll_changed(self, _value=None):
        self._begin_scroll()
        self._request_paint()

    def _scroll_finished(self):
        self._scrolling = False
        self.refresh_data()

    def header_texts(self, column):
        offset = self.offsets[column]
        address = f"+0x{offset:03X}" if offset is not None else "—"
        value = self.header_values(column)[0]
        return self.names[column], address, value

    def _visible_rect(self):
        viewport = self.parentWidget()
        area = viewport.parentWidget() if viewport is not None else None
        if isinstance(area, QScrollArea) and area.widget() is self:
            origin = self.mapFrom(viewport, QPoint())
            return QRectF(origin.x(), origin.y(), viewport.width(), viewport.height()).intersected(QRectF(self.rect()))
        return QRectF(self.rect())

    def marker_tag_rect(self, marker):
        """One tag per marker, pinned to the top of the visible chart."""
        start, end = self._display_time_range()
        stamp = self.markers[marker]
        if stamp is None or not start <= stamp <= end:
            return None
        lane = self.lanes()[0]
        x = lane.left() + (stamp - start) / (end - start) * lane.width()
        left = max(lane.left() + 2, min(x - 22 if marker == 0 else x + 4, lane.right() - 20))
        box = QRectF(left, self._visible_rect().top() + 3, 18, 18)
        if marker == 1:
            other = self.marker_tag_rect(0)
            if other is not None and other.intersects(box):
                box.translate(0, 20)
        return box

    def clear_markers(self):
        changed = any(stamp is not None for stamp in self.markers)
        self.markers = [None, None]
        self._next_marker = 0
        self._drag_marker = None
        self._drag_moved = False
        self._hold_display = False
        self._toggle_marker = None
        self._toggle_stamp = None
        self._marker_press_pos = None
        if changed:
            self.markers_changed.emit(tuple(self.markers))
            self._request_paint()

    def _set_marker(self, marker, stamp):
        # The click was resolved against the frame on screen. Do not let this
        # repaint slide the time axis forward, or the line lands off the cursor.
        self._hold_display = True
        self._mouse_repaint = True
        if self.markers[marker] != stamp:
            self.markers[marker] = stamp
            self.markers_changed.emit(tuple(self.markers))
        self._request_paint()

    def _repaint_marker_change(self):
        """Show a click immediately when every visible curve tile is already cached."""
        if not self.isVisible() or self._scene_key != self._scene_geometry():
            return
        exposed = self._visible_rect().toAlignedRect()
        if (not exposed.isEmpty() and self._lane_scenes
                and all(column in self._lane_scenes for column, _ in self._scene_rows(exposed))):
            # _hold_display keeps this a cached blit plus the marker/readout
            # overlay. Never start an expensive curve render inside an input event.
            self.repaint(exposed)

    def set_highlight(self, column):
        if column != self.highlight:
            self.highlight = column
            self._request_paint()

    def _in_plot(self, position):
        if not self.names or self._on_axis(position):
            return False
        lanes = self.lanes()
        # Marker lines span the gaps between lanes, so their entire shared
        # plotting area must also accept placement, toggling and dragging.
        return QRectF(lanes[0].topLeft(), lanes[-1].bottomRight()).contains(position)

    def _gutter_lane_at(self, position):
        if self._on_axis(position) or not self.names or not 0 <= position.x() < self.LABEL_WIDTH:
            return None
        return next((i for i, lane in enumerate(self.lanes()[:len(self.names)])
                     if lane.top() <= position.y() <= lane.bottom()), None)

    def _sample_time_at(self, position):
        """Time under the pointer on the frame currently drawn.

        The marker line uses this same mapping, so it stays on the click. A
        sample within one pixel keeps that sample's time."""
        if not self._in_plot(position) or self.buffer is None or not len(self.buffer):
            return None
        lane = self.lanes()[0]
        begin, end = self._display_time_range()
        span = end - begin
        if span <= 0 or lane.width() <= 0:
            return None
        stamp = begin + (position.x() - lane.left()) / lane.width() * span
        times = self.buffer.times
        count = self._display_sample_count()
        if not count or stamp < times[0] or stamp > times[count - 1]:
            return None
        index = bisect_left(times, stamp, hi=count)
        if index and (index == count or stamp - times[index - 1] <= times[index] - stamp):
            index -= 1
        sample = times[index]
        sample_x = lane.left() + (sample - begin) / span * lane.width()
        if abs(sample_x - position.x()) <= 1.0:
            return sample
        return stamp

    def _marker_line_x(self, marker):
        begin, end = self._display_time_range()
        stamp = self.markers[marker]
        if stamp is None or end <= begin or not begin <= stamp <= end:
            return None
        lane = self.lanes()[0]
        return lane.left() + (stamp - begin) / (end - begin) * lane.width()

    def _marker_hit(self, position, slop=6):
        """Marker under the pointer, using the drawn line and its tag."""
        line_hit, best = None, slop
        if self._in_plot(position):
            for marker in range(len(self.markers)):
                x = self._marker_line_x(marker)
                if x is None:
                    continue
                distance = abs(x - position.x())
                if distance <= best:
                    best, line_hit = distance, marker
        if line_hit is not None:
            return line_hit, True
        for marker in range(len(self.markers)):
            box = self.marker_tag_rect(marker)
            if box is not None and box.contains(position):
                return marker, False
        return None, False

    def _range_edge_at(self, position, slop=6):
        if self.export_range is None or not self._in_plot(position):
            return None
        begin, end = self._display_time_range()
        if end <= begin:
            return None
        lane = self.lanes()[0]
        hit, best = None, slop
        for edge, stamp in enumerate(self.export_range):
            if not begin <= stamp <= end:
                continue
            x = lane.left() + (stamp - begin) / (end - begin) * lane.width()
            distance = abs(position.x() - x)
            if distance <= best:
                hit, best = edge, distance
        return hit

    def mousePressEvent(self, event):
        position = event.position()
        if event.button() == Qt.MiddleButton and self._in_plot(position):
            # Middle-button panning is independent of marker/range selection.
            # Capture the displayed range on press so live samples arriving
            # during a drag cannot move the view underneath the pointer.
            self.finish_zoom()   # a pan starts from the glide's end, not a frame it would overwrite
            self._pan_press_pos = QPointF(position)
            self._pan_start_range = tuple(self._display_time_range())
            self._pan_dragging = False
            # Keep live painting if the previous pan's frame is still building.
            self._pan_release_pending = False
            self._pan_render_timer.stop()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.RightButton and self._in_plot(position):
            self.export_range = None
            self._range_press = None
            self._range_dragging = False
            self._range_resize = None
            self.clear_markers()
            self.unsetCursor()
            self._request_paint()
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            lane = self._gutter_lane_at(position)
            if lane is not None:
                self.lane_clicked.emit(lane)
                event.accept()
                return
            edge = self._range_edge_at(position)
            if edge is not None:
                self._range_press = None
                self._range_resize = self.export_range[1 - edge]
                self._hold_display = True
                self.setCursor(Qt.SizeHorCursor)
                event.accept()
                return
            hit, on_line = self._marker_hit(position)
            stamp = self._sample_time_at(position)
            self._range_press = None
            if (hit is None and stamp is not None
                    and (self.export_range is None or not self.export_range[0] <= stamp <= self.export_range[1])):
                self._range_press = (QPointF(position), stamp, tuple(self.markers), self._next_marker)
                self._range_dragging = False
            self._toggle_marker = None
            self._toggle_stamp = None
            self._marker_press_pos = None
            if hit is not None and on_line and abs(self._marker_line_x(hit) - position.x()) <= 1.0:
                # Hide on press for immediate feedback. Keep its original time
                # so a subsequent drag can move it or restore an invalid drop.
                self._drag_marker = hit
                self._toggle_marker = hit
                self._toggle_stamp = self.markers[hit]
                self._marker_press_pos = QPointF(position)
                self._drag_moved = False
                self._set_marker(hit, None)
                self._repaint_marker_change()
                event.accept()
                return
            # An empty A or B is still placed, even if the click is near the
            # other line. A badge click selects that marker without moving it.
            adjust = hit is not None and (not on_line or self.markers[self._next_marker] is not None)
            if adjust:
                self._drag_marker = hit
                self._hold_display = True
                self._drag_moved = on_line
                if on_line and stamp is not None:
                    self._set_marker(hit, stamp)
                else:
                    self._request_paint()
                event.accept()
                return
            if stamp is not None:
                marker = self._next_marker
                self._next_marker = 1 - marker
                self._drag_marker = marker
                self._drag_moved = True
                self._set_marker(marker, stamp)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._pan_press_pos is not None:
            self._finish_pan()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._range_resize is not None:
            self.export_range = tuple(sorted((self._range_resize, self._range_time_at(event.position()))))
            self._range_resize = None
            self._hold_display = False
            self._request_paint()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._range_press is not None:
            self._update_export_range(event.position())
            self._range_press = None
            if self._range_dragging:
                self._range_dragging = False
                self._hold_display = False
                self._request_paint()
                event.accept()
                return
        if event.button() == Qt.LeftButton and self._drag_marker is not None:
            marker = self._drag_marker
            moved = self._drag_moved
            toggle = self._toggle_marker == marker
            original_stamp = self._toggle_stamp
            distance = ((event.position() - self._marker_press_pos).manhattanLength()
                        if self._marker_press_pos is not None else 0)
            self._drag_marker = None
            self._drag_moved = False
            self._toggle_marker = None
            self._toggle_stamp = None
            self._marker_press_pos = None
            if toggle and not moved and distance < 3:
                self._set_marker(marker, None)
                self._next_marker = marker if any(stamp is not None for stamp in self.markers) else 0
            else:
                stamp = self._sample_time_at(event.position()) if moved or toggle and distance >= 3 else None
                if stamp is not None:
                    self._set_marker(marker, stamp)
                elif toggle and self.markers[marker] is None:
                    self._set_marker(marker, original_stamp)
                else:
                    self._hold_display = False
                    self._request_paint()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        self._hover = event.position().x()
        self._mouse_repaint = True
        if self._pan_press_pos is not None:
            if event.buttons() & Qt.MiddleButton:
                self._pan_view(event.position())
                event.accept()
                return
            self._finish_pan()   # release lost, e.g. focus switched mid-drag
        if self._range_resize is not None and event.buttons() & Qt.LeftButton:
            self.export_range = tuple(sorted((self._range_resize, self._range_time_at(event.position()))))
            self._request_paint()
            event.accept()
            return
        if self._range_press is not None and event.buttons() & Qt.LeftButton:
            self._update_export_range(event.position())
            if self._range_dragging:
                self._request_paint()
                return
        if self._range_edge_at(event.position()) is not None:
            self.setCursor(Qt.SizeHorCursor)
        elif self._gutter_lane_at(event.position()) is None:
            self.unsetCursor()
        else:
            self.setCursor(Qt.PointingHandCursor)
        if self._drag_marker is not None and event.buttons() & Qt.LeftButton:
            if (self._toggle_marker is not None and not self._drag_moved
                    and (event.position() - self._marker_press_pos).manhattanLength() < 3):
                self._request_paint()
                return
            stamp = self._sample_time_at(event.position())
            if stamp is not None:
                self._drag_moved = True
                self._set_marker(self._drag_marker, stamp)
        self._request_paint()

    def _pan_view(self, position):
        """Move the displayed time window horizontally while the middle button is held."""
        if (self._pan_press_pos is None or self._pan_start_range is None
                or self.buffer is None or not len(self.buffer)):
            return
        lane = self.lanes()[0]
        if lane.width() <= 0:
            return
        start, end = self._pan_start_range
        span = max(0.0, end - start)
        if span <= 0:
            return
        moved = position.x() - self._pan_press_pos.x()
        if not self._pan_dragging and abs(moved) < 3:
            return   # a plain middle click must not park the live view
        self._pan_dragging = True
        delta = moved / lane.width() * span
        desired = start - delta
        earliest, latest = float(self.buffer.times[0]), float(self.buffer.times[-1])
        # Keep the window over retained samples.  If the minimum zoom span is
        # wider than the retained history, pin it to the earliest sample.
        if span >= latest - earliest:
            begin = earliest
        else:
            begin = max(earliest, min(desired, latest - span))
        view = (begin, begin + span)
        if view == self._view_range:
            return
        self._view_range = view
        # Use the same deferred frame path as vertical scrolling so a drag
        # remains responsive when many lanes or a dense sample history are
        # visible.
        self._begin_scroll()
        self._prepare_timer.stop()
        self._prepared_frame = None
        self._tile_timer.stop()
        self._pending_tiles.clear()
        self._scene_cache = None
        self._pan_live_active = True
        # Coalesce high-frequency mouse moves and paint only the newest range.
        # The eventual frame is still rendered from the current buffer; this
        # prevents a slow paint from blocking and queuing every intermediate
        # pointer position.
        if not self._pan_render_timer.isActive():
            # Pace to ~60 fps from the last frame, not from this move; a fixed
            # 16 ms after each move capped the drag near 40 fps.
            elapsed = (time.perf_counter() - self._pan_painted) * 1000
            self._pan_render_timer.start(max(0, round(16 - elapsed)))

    def _finish_pan(self):
        self._pan_press_pos = None
        self._pan_start_range = None
        self._pan_dragging = False
        if self._pan_live_active:
            # Keep drawing the current range while the final cached frame
            # is prepared after release.
            self._pan_release_pending = True
        self._pan_render_timer.stop()
        self.unsetCursor()
        self._request_paint()
        self._flush_pan_render()

    def _flush_pan_render(self):
        if not self._pan_live_active:
            return
        exposed = self._visible_rect().toAlignedRect()
        if exposed.isValid() and not exposed.isEmpty():
            self._pan_painted = time.perf_counter()
            self.repaint(exposed)

    def _update_export_range(self, position):
        origin, stamp, markers, next_marker = self._range_press
        if not self._range_dragging and abs(position.x() - origin.x()) < 3:
            return
        self._range_dragging = True
        self._drag_marker = None
        self.markers = list(markers)
        self._next_marker = next_marker
        self.markers_changed.emit(tuple(self.markers))
        self._hold_display = True
        self.export_range = tuple(sorted((stamp, self._range_time_at(position))))

    def _range_time_at(self, position):
        stamp = self._sample_time_at(position)
        if stamp is not None:
            return stamp
        lane = self.lanes()[0]
        begin, end = self._display_time_range()
        x = max(lane.left(), min(position.x(), lane.right()))
        last = begin + (x - lane.left()) / lane.width() * (end - begin)
        return max(self.buffer.times[0], min(last, self.buffer.times[-1]))

    def leaveEvent(self, event):
        self._hover = None
        self._mouse_repaint = True
        self._request_paint()
        super().leaveEvent(event)

    def _paint_chart(self, painter, exposed, start, end, frame=None):
        painter.setRenderHint(QPainter.Antialiasing)
        span = end - start
        lanes = self.lanes()
        times = self.buffer.times if self.buffer else array("d")
        count = self._frame_count if frame is None else frame["count"]
        # A frame captured before the buffer shrank (a drop commits between the
        # snapshot and this paint) must never index past the retained arrays.
        if count > len(times):
            count = len(times)
        revision = self._scene_revision if frame is None else frame["revision"]
        paths = self._trace_cache if frame is None else frame["paths"]
        first = max(0, bisect_right(times, start, hi=count) - 1)   # the sample held into view
        last = bisect_right(times, end, hi=count)
        cache_key = (id(self.buffer), revision, self.signed,
                     start, end, self.width(), self.height())
        if frame is None and cache_key != self._cache_key:
            self._cache_key = cache_key
            self._trace_cache.clear()
        small = QFont(self.font())
        small.setPointSize(8)
        step, ticks = self._time_ticks(start, end)
        for column, (lane, name) in enumerate(zip(lanes, self.names)):
            # A long chart is scrolled inside the dialog. Only build and draw
            # traces in the exposed region, rather than all 32 lanes each frame.
            if lane.bottom() < exposed.top() or lane.top() > exposed.bottom():
                continue
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
            for begin, finish in self.buffer.pauses if self.buffer else ():
                left = lane.left() + max(0.0, (begin - start) / span) * lane.width()
                right = lane.left() + min(1.0, (finish - start) / span) * lane.width()
                if right > left:
                    painter.fillRect(QRectF(left, lane.top() + 1, right - left, lane.height() - 2), PAUSE_FILL)
            gutter =QRectF(8, lane.top(), self.LABEL_WIDTH - 16, lane.height())
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(gutter.left(), gutter.top() + 5, 4, max(4.0, lane.height() - 10)), 2, 2)
            painter.setPen(QColor("#23374A"))
            painter.setFont(self.font())
            text_rect = gutter.adjusted(10, 2, 0, 0)
            header_name = self.names[column]
            offset = self.offsets[column]
            header_address = f"+0x{offset:03X}" if offset is not None else "—"
            suffix = f"  {header_address}"
            width = max(0, int(text_rect.width()) - painter.fontMetrics().horizontalAdvance(suffix))
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignTop,
                             painter.fontMetrics().elidedText(header_name, Qt.ElideRight, width) + suffix)
            source = self.columns[column]
            if source is None or not times or not last or start > times[count - 1]:
                continue
            values = self.buffer.column(source, self.signed)
            cached = paths.get(column)
            low, high = (cached[:2] if cached else self.buffer.extrema(source, first, last, self.signed))
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
            trace_end = min(count, last + 1)   # complete the held step, using this frame's samples
            dense = trace_end - first > lane.width() * 2
            # Dense envelopes repeatedly retrace a vertical pixel column. Qt's
            # antialiased wide-pen stroker is costly for these degenerate segments.
            painter.setRenderHint(QPainter.Antialiasing, not dense)
            painter.setPen(QPen(color, 1.0 if dense else 1.4))
            painter.setBrush(Qt.NoBrush)
            if cached:
                path, marks = cached[2:]
            else:
                direct = trace_end - first <= self.DIRECT_TRACE
                path = QPolygonF(self.trace(times, values, first, start, span, lane, y_of,
                                            last=trace_end)) if direct else None
                marks = []
                failures = self.buffer.failures[source]
                if failures:
                    width = max(1, int(lane.width()))
                    for pixel in range(width):
                        lo = bisect_left(times, start + pixel / width * span, first, last)
                        hi = bisect_right(times, start + (pixel + 1) / width * span, lo, last)
                        at = bisect_left(failures, lo)
                        if at < len(failures) and failures[at] < hi:
                            marks.append(lane.left() + pixel + 0.5)
                paths[column] = (data_low, data_high, path, marks)
            if path is None:
                painter.save()
                self._paint_cached_trace(painter, source, lane, start, span, first, trace_end, count,
                                         bottom_value, top_value, dense)
                painter.restore()
            elif len(path) == 1:
                painter.drawEllipse(path[0], 2, 2)
            else:
                painter.drawPolyline(path)
            failures = self.buffer.failures[source]
            if failures:
                painter.setPen(QPen(QColor("#C44848"), 1))
                for x in marks:
                    painter.drawLine(QPointF(x, lane.bottom() - 6), QPointF(x, lane.bottom() - 1))
            painter.restore()

    DIRECT_TRACE = 64       # fewer samples are traced directly, keeping the single-sample dot
    TRACE_CHUNK = 256       # samples (sparse) or min/max blocks (dense) per cached piece
    TRACE_CACHE = 768       # cached pieces, all lanes and levels

    def _paint_cached_trace(self, painter, source, lane, start, span, first, end, count, bottom_value, top_value,
                            dense):
        """Trace in (time, value) space from cached pieces, so zoom, pan and live frames mostly
        reuse points. Sparse: the step trace. Dense: a min/max zigzag of 2**k-sample blocks,
        at most one block per pixel."""
        buffer = self.buffer
        times, values, base = buffer.times, buffer.column(source, self.signed), buffer.dropped
        level = math.ceil(math.log2((end - first) / max(1.0, lane.width()))) if dense else 0
        size = self.TRACE_CHUNK << level
        limit = base + count
        lo = max(base, (first + base) // size * size)
        hi = min(limit, -(-(end + base) // size) * size)
        resumes = {bisect_left(times, resumed) + base for _, resumed in buffer.pauses}
        cuts = sorted({lo, hi, *range(lo - lo % size + size, hi, size), *(r for r in resumes if lo < r < hi)})
        points = []
        for a, b in zip(cuts, cuts[1:]):
            if points:   # hold the previous sample up to this piece, also across a pause
                held = values[a - 1 - base]
                points += (QPointF(times[a - 1 - base], held), QPointF(times[a - base], held))
            points += self._trace_piece(source, level, a, b, b < limit or b % size == 0)
        pen = painter.pen()
        pen.setCosmetic(True)   # width in pixels despite the value-space transform
        pen.setWidthF(1.0)      # antialiased pens above 1 px take Qt's ~20x slower stroker
        painter.setPen(pen)
        sx, sy = lane.width() / span, -lane.height() / (top_value - bottom_value)
        painter.setTransform(QTransform(sx, 0, 0, sy, lane.left() - start * sx, lane.bottom() - bottom_value * sy),
                             True)
        polygon = QPolygonF(points)   # one polyline: antialiased joints blend once
        painter.drawPolyline(polygon)
        if not dense:   # a second stroke offset by under half a pixel reads as the 1.4 px trace
            painter.setTransform(painter.transform() * QTransform.fromTranslate(0.45, 0.45))
            painter.drawPolyline(polygon)

    def _trace_piece(self, source, level, a, b, final):
        """Points of absolute samples a..b-1: level 0 steps, else (x, max), (x, min) per block."""
        key = (source, self.signed, level, a, b)
        points = self._piece_cache.get(key)
        if points is not None:
            self._piece_cache.move_to_end(key)
            return points
        buffer = self.buffer
        times, values, base = buffer.times, buffer.column(source, self.signed), buffer.dropped
        if not level:
            xs, ys = times[a - base:b - base], values[a - base:b - base]
            points = [QPointF(xs[0], ys[0])]
            points += chain.from_iterable(zip(map(QPointF, xs[1:], ys), map(QPointF, xs[1:], ys[1:])))
        else:
            width = 1 << level
            j0, j1 = -(-a // width), b // width
            points = []

            def partial(lo, hi):
                chunk = values[lo - base:hi - base]
                x = times[lo - base]
                points.extend((QPointF(x, max(chunk)), QPointF(x, min(chunk))))

            if j0 >= j1:
                partial(a, b)
            else:
                if a < j0 * width:
                    partial(a, j0 * width)
                origin, mins, maxs = buffer.blocks(source, self.signed, level)
                xs = times[j0 * width - base:j1 * width - base:width]
                points += chain.from_iterable(zip(map(QPointF, xs, maxs[j0 - origin:j1 - origin]),
                                                  map(QPointF, xs, mins[j0 - origin:j1 - origin])))
                if j1 * width < b:
                    partial(j1 * width, b)
        if final:   # the growing tail piece changes with every batch
            self._piece_cache[key] = points
            while len(self._piece_cache) > self.TRACE_CACHE:
                self._piece_cache.popitem(last=False)
        return points

    def paint_pinned_axis(self, painter, band, start=None, end=None):
        """Draw the shared time axis into ``band``. Tick x matches the chart lanes."""
        if start is None:
            start, end = self._display_time_range()
        if band.height() < 8 or end <= start or not self.names:
            return
        painter.save()
        painter.setClipRect(band)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRect(band)
        painter.setPen(QPen(QColor("#DEE5EC"), 1))
        painter.drawLine(QPointF(band.left(), band.top()), QPointF(band.right(), band.top()))
        step, ticks = self._time_ticks(start, end)
        small = QFont(self.font())
        small.setPointSize(8)
        painter.setFont(small)
        painter.setPen(QColor("#718397"))
        span = end - start
        lane = self.lanes()[0]
        scale = lane.width() / span
        for tick in ticks:
            x = lane.left() + (tick - start) * scale
            painter.drawLine(QPointF(x, band.top()), QPointF(x, band.top() + 5))
            painter.drawText(QRectF(x - 40, band.top() + 6, 80, 16), Qt.AlignCenter, format_seconds(tick, step))
        if self._hover is not None and lane.left() <= self._hover <= lane.right():
            stamp = start + (self._hover - lane.left()) * span / lane.width()
            box = QRectF(self._hover - 45, band.top() + 4, 90, 16)
            painter.setFont(small)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#203747"))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(box, Qt.AlignCenter, format_seconds(stamp, step / 100))
        painter.restore()

    def _paint_axis(self, painter, start, end):
        """In-chart axis, used when the dialog has not pinned one below the viewport."""
        if self.axis_view is not None:
            return
        self.paint_pinned_axis(painter, self.axis_band(), start, end)

    def _paint_readouts(self, painter, exposed, start, end):
        lanes = self.lanes()
        span = end - start
        if self.export_range is not None and span > 0:
            first, last = self.export_range
            left = max(start, first)
            right = min(end, last)
            if left <= right:
                for lane in lanes:
                    if lane.bottom() < exposed.top() or lane.top() > exposed.bottom():
                        continue
                    x = lane.left() + (left - start) / span * lane.width()
                    width = (right - left) / span * lane.width()
                    painter.fillRect(QRectF(x, lane.top(), max(1., width), lane.height()),
                                     QColor(36, 99, 220, 45))
                    for stamp in (first, last):
                        if start <= stamp <= end:
                            edge_x = lane.left() + (stamp - start) / span * lane.width()
                            painter.setPen(QPen(QColor("#2463DC"), 1.4))
                            painter.drawLine(QPointF(edge_x, lane.top()), QPointF(edge_x, lane.bottom()))
                            painter.fillRect(QRectF(edge_x - 2, lane.center().y() - 6, 4, 12), QColor("#2463DC"))
        hover = self._hover_index()
        indices = (hover, *(self._index_at(stamp) for stamp in self.markers))
        pointer = self._hover is not None and lanes[0].left() <= self._hover <= lanes[0].right()
        mono = QFont("Consolas", 9)
        painter.setRenderHint(QPainter.Antialiasing)
        for column, lane in enumerate(lanes[:len(self.names)]):
            if lane.bottom() < exposed.top() or lane.top() > exposed.bottom():
                continue
            color = QColor(SERIES_COLORS[column % len(SERIES_COLORS)])
            values = self.readout_values(column, indices)
            text_rect = QRectF(18, lane.top() + 2, self.LABEL_WIDTH - 26, lane.height() - 2)
            painter.setFont(mono)
            for line, (caption, value) in enumerate(zip(("N", "S", "A", "B"), values)):
                readout_color = color if line < 2 else QColor(("#B57518", "#7A4FC4")[line - 2])
                painter.setPen(QColor("#C44848") if value == "读取失败" else readout_color)
                painter.drawText(text_rect.adjusted(0, 17 + line * 17, 0, 0), Qt.AlignLeft | Qt.AlignTop,
                                 f"{caption}: {value}")
            if not pointer:
                continue
            # The line follows the pointer continuously; only its value is
            # quantized to the preceding sample on the step trace.
            x = self._hover
            painter.setPen(QPen(QColor("#9CAFBD"), 1, Qt.DashLine))
            painter.drawLine(QPointF(x, lane.top()), QPointF(x, lane.bottom()))
            text = values[1]
            width = painter.fontMetrics().horizontalAdvance(text) + 10
            height = 17
            if width > lane.width() - 4:
                parts = text.split(" · ")
                text = "\n".join(parts)
                width = max(painter.fontMetrics().horizontalAdvance(part) for part in parts) + 10
                height = 34
            left = x + 6 if x + 6 + width < lane.right() else x - 6 - width
            box = QRectF(max(lane.left() + 2, left), lane.bottom() - height - 4, width, height)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 225))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor("#C44848") if text == "读取失败" else color)
            painter.drawText(box, Qt.AlignCenter, text)
        for marker, stamp in enumerate(self.markers):
            if stamp is None or not start <= stamp <= end:
                continue
            x = lanes[0].left() + (stamp - start) / span * lanes[0].width()
            color = QColor(("#B57518", "#7A4FC4")[marker])
            painter.setPen(QPen(color, 1.4, Qt.DashLine))
            painter.drawLine(QPointF(x, lanes[0].top()), QPointF(x, lanes[-1].bottom()))
            box = self.marker_tag_rect(marker)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(box, Qt.AlignCenter, "AB"[marker])
        self._paint_axis(painter, start, end)

    def _scene_geometry(self):
        return (self._transform_key(), self.signed, self.highlight, self.devicePixelRatioF(), self.font().toString())

    def _scene_rows(self, exposed):
        lanes = self.lanes()[:len(self.names)]
        rows = []
        for column, lane in enumerate(lanes):
            top = 0. if column == 0 else lane.top() - 4
            rect = QRectF(0, top, self.width(), lane.bottom() + 4 - top).toAlignedRect()
            if rect.intersects(exposed):
                rows.append((column, rect))
        return rows

    def _render_lane(self, rect, ratio, frame=None):
        image = QPixmap(max(1, math.ceil(rect.width() * ratio)), max(1, math.ceil(rect.height() * ratio)))
        image.setDevicePixelRatio(ratio)
        image.fill(QColor("#FFFFFF"))
        chart = QPainter(image)
        try:
            chart.translate(-rect.x(), -rect.y())
            interval = self._scene_range if frame is None else frame["range"]
            self._paint_chart(chart, rect, *interval, frame=frame)
        finally:
            chart.end()
        return image

    def _start_prepare(self, geometry, exposed, ratio):
        if self._prepared_frame is not None:
            # Appended samples do not invalidate this frame's bounded indices.
            # Restarting on every batch starved slow frames before all lanes finished.
            if self._prepared_frame["geometry"] == geometry:
                if not self._prepare_timer.isActive():
                    self._prepare_timer.start(0)
                return
            self._prepare_timer.stop()
            self._prepared_frame = None
        self._tile_timer.stop()
        self._pending_tiles.clear()
        self._prepared_frame = {"geometry": geometry, "revision": self.buffer.revision if self.buffer else 0,
                                "count": len(self.buffer) if self.buffer is not None else 0,
                                "range": self.time_range(), "ratio": ratio, "paths": {}, "tiles": OrderedDict(),
                                "rows": self._scene_rows(exposed), "render_ms": 0.}
        self._prepare_timer.start(0)

    def _interaction_frozen(self):
        return (self._drag_marker is not None or self._range_dragging
                or self._range_resize is not None or self._hold_display)

    def _prepare_one_lane(self):
        frame = self._prepared_frame
        if frame is None:
            return
        if self._interaction_frozen():
            # Leave the frame queued. Dropping the timer here made the next
            # data refresh a no-op, so a marker click froze the live chart.
            self._prepare_timer.start(30)
            return
        if not self.isVisible() or frame["geometry"] != self._scene_geometry():
            self._prepared_frame = None
            if self.isVisible():
                self._request_paint()
            return
        column, rect = frame["rows"].pop(0)
        started = time.perf_counter()
        frame["tiles"][column] = self._render_lane(rect, frame["ratio"], frame)
        frame["render_ms"] += (time.perf_counter() - started) * 1000
        if frame["rows"]:
            self._prepare_timer.start(0)
            return
        # Commit the whole frame together so curves and their time axis never
        # mix snapshots. Input can run between the short per-lane render jobs.
        self._scene_key = frame["geometry"]
        self._scene_transform = frame["geometry"][0]
        self._scene_revision = frame["revision"]
        self._frame_count = frame["count"]
        self._scene_range = frame["range"]
        self._lane_scenes = frame["tiles"]
        self._pan_live_active = False
        self._pan_release_pending = False
        self._trace_cache = frame["paths"]
        self._cache_key = (id(self.buffer), self._scene_revision, self.signed, *self._scene_range,
                           self.width(), self.height())
        self._frame_render_ms = frame["render_ms"]
        self._frame_created = time.monotonic()
        self._scene_rect = None
        self._prepared_frame = None
        self._tile_timer.stop()
        self._pending_tiles.clear()
        self._request_paint()

    def _tile_frame_key(self):
        return (self._scene_key, self._scene_revision, self._frame_count, self._scene_range)

    def _queue_tiles(self, exposed, ratio):
        """Prepare visible cache misses first, then nearby rows during a scroll."""
        visible = self._scene_rows(exposed)
        wanted = OrderedDict(visible)
        if self._scrolling:
            margin = 2 * (self.LANE_MIN_HEIGHT + 8)
            for column, rect in self._scene_rows(exposed.adjusted(0, -margin, 0, margin)):
                wanted.setdefault(column, rect)
        key = self._tile_frame_key()
        self._pending_tiles = OrderedDict(
            (column, (rect, ratio, key)) for column, rect in wanted.items()
            if column not in self._lane_scenes or abs(self._lane_scenes[column].devicePixelRatio() - ratio) > .01)
        if self._pending_tiles and not self._tile_timer.isActive():
            self._tile_timer.start(1)
        elif not self._pending_tiles:
            self._tile_timer.stop()

    def _prepare_tile(self):
        if not self.isVisible() or not self._pending_tiles:
            self._pending_tiles.clear()
            return
        if self._drag_marker is not None or self._hold_display:
            self._tile_timer.start(30)
            return
        column, (rect, ratio, key) = self._pending_tiles.popitem(last=False)
        if key != self._tile_frame_key() or self._scene_key != self._scene_geometry():
            self._pending_tiles.clear()
            self._request_paint()
            return
        # This job runs between input/paint events, never inside a wheel repaint.
        # Appends keep the captured prefix valid; buffer drops invalidate the key.
        self._lane_scenes[column] = self._render_lane(rect, ratio)
        self._lane_scenes.move_to_end(column)
        self._request_paint()
        if self._pending_tiles:
            self._tile_timer.start(1)

    def _compose_scene(self, exposed, ratio):
        """Reuse individual lane images across changes to the vertical viewport."""
        rows = self._scene_rows(exposed)
        image = QPixmap(max(1, math.ceil(exposed.width() * ratio)), max(1, math.ceil(exposed.height() * ratio)))
        image.setDevicePixelRatio(ratio)
        image.fill(QColor("#FFFFFF"))
        target = QPainter(image)
        try:
            target.translate(-exposed.x(), -exposed.y())
            for column, rect in rows:
                tile = self._lane_scenes.get(column)
                if tile is None:
                    tile = self._render_lane(rect, ratio)
                    self._lane_scenes[column] = tile
                self._lane_scenes.move_to_end(column)
                target.drawPixmap(rect.topLeft(), tile)
            # Keep the visible rows plus a small recent margin, rather than a
            # full-height image of all 32 registers at high DPI.
            while len(self._lane_scenes) > max(8, len(rows) + 2):
                self._lane_scenes.popitem(last=False)
        finally:
            target.end()
        return image

    def _blit_lanes(self, painter, exposed, ratio):
        """Draw cached lanes; defer missing curves instead of blocking scrolling."""
        rows = self._scene_rows(exposed)
        missing = any(column not in self._lane_scenes
                      or abs(self._lane_scenes[column].devicePixelRatio() - ratio) > .01
                      for column, _ in rows)
        if missing:
            # Show labels/grid immediately while the next short tile job prepares
            # the curve. A count of zero skips every sample/extrema/trace scan.
            self._paint_chart(painter, exposed, *self._scene_range,
                              frame={"count": 0, "revision": self._scene_revision, "paths": {}})
        self._queue_tiles(exposed, ratio)
        for column, rect in rows:
            tile = self._lane_scenes.get(column)
            if tile is None or abs(tile.devicePixelRatio() - ratio) > 0.01:
                continue
            self._lane_scenes.move_to_end(column)
            painter.drawPixmap(rect.topLeft(), tile)
        if not self._scrolling:
            while len(self._lane_scenes) > max(8, len(rows) + 2):
                self._lane_scenes.popitem(last=False)

    def _finish_paint_flags(self, freeze):
        self._mouse_repaint = False
        if self._drag_marker is None and not self._range_dragging and self._range_resize is None:
            self._hold_display = False
        if not freeze:
            self._data_refresh_requested = False
        if self._prepared_frame is not None and not self._interaction_frozen() and not self._prepare_timer.isActive():
            self._prepare_timer.start(0)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setClipRect(event.rect())
        painter.fillRect(event.rect(), QColor("#FFFFFF"))
        if not self.names:
            painter.setPen(QColor("#91A0AF"))
            painter.drawText(self.rect(), Qt.AlignCenter, "在左侧勾选寄存器，点「开始监视」绘制曲线")
            return
        # Trace work stays inside the viewport. Rebuilding every lane on a scroll
        # tick is what made the wheel feel late once sampling had started.
        exposed = self._visible_rect().toAlignedRect()
        if exposed.isEmpty():
            exposed = QRectF(event.rect()).intersected(QRectF(self.rect())).toAlignedRect()
        if exposed.isEmpty():
            return
        ratio = self.devicePixelRatioF()
        if self._pan_live_active:
            # A pan frame is rendered from the current buffer and current
            # range. This keeps the trace accurate while the pointer moves;
            # the cached tile frame is prepared only after release.
            if self._pan_release_pending and self._prepared_frame is None:
                self._start_prepare(self._scene_geometry(), exposed, ratio)
            count = len(self.buffer) if self.buffer is not None else 0
            start, end = self.time_range()
            self._paint_chart(painter, exposed, start, end,
                              frame={"count": count,
                                     "revision": self.buffer.revision if self.buffer is not None else 0,
                                     "paths": {}})
            self._paint_readouts(painter, exposed, start, end)
            self._finish_paint_flags(False)
            self._sync_axis_view()
            return
        if self._zoom_target is not None:
            # Glide frames go straight to the screen; the tiled frame is built once it settles.
            start, end = self.time_range()
            self._paint_chart(painter, exposed, start, end,
                              frame={"count": len(self.buffer) if self.buffer is not None else 0,
                                     "revision": self.buffer.revision if self.buffer is not None else 0,
                                     "paths": {}})
            self._paint_readouts(painter, exposed, start, end)
            self._finish_paint_flags(False)
            self._sync_axis_view()
            return
        transform = self._transform_key()
        geometry = self._scene_geometry()
        revision = self.buffer.revision if self.buffer is not None else 0
        changed = revision != self._scene_revision
        # Marker gestures keep their time coordinates; vertical scrolling stays live.
        freeze = self._interaction_frozen()
        structural = self._scene_key is None or geometry != self._scene_key
        if self._scrolling:
            if structural:
                # A resize, lane change or retained-buffer drop invalidates the
                # old frame. Start fresh metadata, but leave curve work deferred.
                self._tile_timer.stop()
                self._pending_tiles.clear()
                self._scene_range = self.time_range()
                self._scene_transform = transform
                self._scene_key = geometry
                self._scene_revision = revision
                self._frame_count = len(self.buffer) if self.buffer is not None else 0
                self._lane_scenes.clear()
                self._trace_cache.clear()
                self._cache_key = None
                self._scene_cache = None
            elif changed and not freeze:
                # Prepare visible lanes between input events and commit their
                # time axis together. Wheel paints never render the traces.
                self._start_prepare(geometry, exposed, ratio)
            self._blit_lanes(painter, exposed, ratio)
            self._paint_readouts(painter, exposed, *self._scene_range)
            self._finish_paint_flags(freeze)
            self._sync_axis_view()
            return
        update_data = (not freeze and changed and (not self._mouse_repaint or self._data_refresh_requested))
        if not structural and update_data and self._frame_render_ms >= 12 and self.isVisible():
            self._start_prepare(geometry, exposed, ratio)
            update_data = False
        new_frame = structural or update_data
        use_blit = (not new_frame and self._scene_range is not None
                    and self._scene_transform == transform)
        if use_blit:
            self._blit_lanes(painter, exposed, ratio)
            self._paint_readouts(painter, exposed, *self._scene_range)
            self._finish_paint_flags(freeze)
            self._sync_axis_view()
            return
        if new_frame:
            self._tile_timer.stop()
            self._pending_tiles.clear()
            self._prepare_timer.stop()
            self._prepared_frame = None
            self._scene_range = self.time_range()
            self._scene_transform = transform
            self._scene_key = geometry
            self._scene_revision = revision
            self._frame_count = len(self.buffer) if self.buffer is not None else 0
            self._lane_scenes.clear()
        if new_frame or exposed != self._scene_rect or self._scene_cache is None:
            started = time.perf_counter()
            self._scene_cache = self._compose_scene(exposed, ratio)
            self._scene_rect = exposed
            if new_frame:
                self._frame_render_ms = (time.perf_counter() - started) * 1000
                self._frame_created = time.monotonic()
        if self._scene_cache is not None:
            painter.drawPixmap(exposed.topLeft(), self._scene_cache)
        if self._scene_range is not None:
            self._paint_readouts(painter, exposed, *self._scene_range)
        self._finish_paint_flags(freeze)
        self._sync_axis_view()

    def _sync_axis_view(self):
        # A synchronous footer repaint from inside paintEvent re-enters the chart
        # and stalls the click that places a marker.
        if self.axis_view is None or self._axis_sync_pending:
            return
        self._axis_sync_pending = True
        QTimer.singleShot(0, self._flush_axis_view)

    def _flush_axis_view(self):
        self._axis_sync_pending = False
        if self.axis_view is not None:
            self.axis_view.update()


class _MonitorBridge(QObject):
    started = Signal(int, bool)
    samples = Signal(int, object)
    stopped = Signal(int, str)
    sampler = Signal(int, str)
    export_failed = Signal(int, str)


class _MonitorRegisterDelegate(QStyledItemDelegate):
    """Toggle immediately on press across the whole row; mark the focused row."""

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
            if event.type() == QEvent.MouseButtonPress:
                checked = index.data(Qt.CheckStateRole) == Qt.Checked.value
                return model.setData(index, Qt.Unchecked.value if checked else Qt.Checked.value, Qt.CheckStateRole)
            return True
        return super().editorEvent(event, model, option, index)


class _PinnedTimeAxis(QWidget):
    """Time axis fixed under the scrolling chart so it stays visible with many lanes."""

    def __init__(self, plot, parent=None):
        super().__init__(parent)
        self.plot = plot
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor("#FFFFFF"))
        self.plot.paint_pinned_axis(painter, QRectF(self.rect()))

    def wheelEvent(self, event):
        area = self.parentWidget()
        if isinstance(area, QScrollArea) and not event.modifiers() & Qt.ControlModifier:
            self.plot._begin_scroll()
            area.verticalScrollBar().wheelEvent(event)
            event.accept()
            return
        if event.modifiers() & Qt.ControlModifier:
            self.plot.wheelEvent(event)
            return
        super().wheelEvent(event)


class MonitorDialog(ManagedDialog):
    """Pick registers of the current component, sample them on the board and plot them."""
    settings_changed = Signal(dict)

    def __init__(self, session_provider, parent=None, settings=None):
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
        self._selection_kind = None
        self._epoch = 0
        self._runs = 0   # bumped only by start(): stop alone must not hide an export's outcome
        self._stop = threading.Event()
        self._workers = set()
        self._dirty = False
        self.state = "idle"
        self.buffer = None
        self.monitored = []   # (name, address) of the current/last run
        self.monitored_offsets = []
        self._shown = None    # idle lanes when they differ from the run, see shown
        self.sampler = ""     # board-side sampler of the current/last run, see SAMPLER_LABELS
        self._last_data = None   # monotonic time of the latest samples of this run
        self._shown_pause = 0
        self.interval_ms = self.settings["interval_ms"]   # period of the current/last run
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.export_pool = QThreadPool(self)
        self.export_pool.setMaxThreadCount(1)
        self._exporting = False
        self.bridge = _MonitorBridge(self)
        self.bridge.started.connect(self._started)
        self.bridge.samples.connect(self._samples)
        self.bridge.stopped.connect(self._stopped)
        self.bridge.sampler.connect(self._sampler_known)
        self.bridge.export_failed.connect(self._export_failed)

        layout = QVBoxLayout(self)
        layout.setVerticalSizeConstraint(QLayout.SetMinimumSize)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        self.component_label = ElidedLabel("", "muted")
        self.component_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        control_width = 110
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(MONITOR_MIN_INTERVAL_MS, MONITOR_MAX_INTERVAL_MS)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setFixedWidth(control_width)
        self.interval_spin.setValue(self.settings["interval_ms"])
        self.interval_spin.valueChanged.connect(self._interval_changed)
        self.start_button = button("开始监视", self.toggle, "primary", "play")
        self.start_button.setFixedWidth(control_width)
        self._interval_tooltip()
        self.pause_button = button("暂停", self.toggle_pause, None, "pause")
        self.pause_button.setProperty("outlined", True)
        self.pause_button.setFixedWidth(control_width)
        self.pause_button.setEnabled(False)
        self.pause_button.setToolTip("暂停板端采样并保留已采数据，可缩放、打标记、导出；继续后接着画在同一条曲线上，暂停时段以浅色底标出")
        # Header: title on the left, run controls on the right.
        header = row(label("寄存器监视", "title"), self.component_label, label("采样间隔", "muted"),
                     self.interval_spin, self.pause_button, self.start_button, spacing=8)
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
        self.list.setUniformItemSizes(True)
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
        self.export_button = button("导出数据", self.export_data, "flat", "export")
        self.export_button.setToolTip("左键拖选曲线时间段，拖动选区左右边缘调整范围，在选区外拖动重新选择；仅导出选区，未选则导出全部保留数据，右键清除选区。支持 ZIP 或 CSV")
        for control in (self.clear_button, self.export_button):
            control.setFixedWidth(control_width)
        self.plot = MonitorPlot()
        self.plot.sample_interval_s = self.interval_ms / 1000
        self.plot.signed = self.settings["signed"]
        self.zoom_label = label("100%", "muted")
        self.zoom_label.setMinimumWidth(58)
        self.zoom_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_button = button("Zoom", lambda: self.plot.set_zoom(1.0), "flat", "axis")
        self.zoom_button.setToolTip("查看全局图表：恢复 100% 比例，显示全部保留的数据。\nCtrl + 滚轮以鼠标位置为中心缩放，最小窗口为两倍采样间隔；按住中键拖动平移。")
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
        self.plot_scroll = QScrollArea()
        self.plot_scroll.setFrameShape(QFrame.NoFrame)
        self.plot_scroll.setWidgetResizable(True)
        self.plot_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.plot_scroll.setMinimumSize(self.plot.minimumWidth(), 220)
        self.plot_scroll.setWidget(self.plot)
        self.plot_scroll.setViewportMargins(0, 0, 0, self.plot.AXIS_HEIGHT)
        self.axis_footer = _PinnedTimeAxis(self.plot, self.plot_scroll)
        self.plot.axis_view = self.axis_footer
        self.plot.installEventFilter(self)
        self.plot_scroll.installEventFilter(self)
        self.plot_scroll.viewport().installEventFilter(self)
        # Tags are pinned to the viewport, so a scroll still repaints that strip.
        # The plot only blits cached lanes; it does not rebuild traces mid-gesture.
        self.plot_scroll.verticalScrollBar().valueChanged.connect(self.plot.scroll_changed)
        self.plot_scroll.verticalScrollBar().rangeChanged.connect(lambda *_: self._position_axis_footer())
        main.addWidget(self.plot_scroll, 1)
        self._position_axis_footer()
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
        self.refresh_timer.setInterval(16)   # draw each ~40 ms board batch on arrival, without a beat
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
        if hasattr(self, "axis_footer"):
            self._position_axis_footer()

    def _position_axis_footer(self):
        viewport = self.plot_scroll.viewport()
        geo = viewport.geometry()
        self.axis_footer.setGeometry(geo.x(), geo.bottom() + 1, viewport.width(), self.plot.AXIS_HEIGHT)
        self.axis_footer.raise_()

    # -- register list -------------------------------------------------------------------
    def set_registers(self, title, registers, preselect=(), key=None, kind=None):
        """List a component's registers; ignored while a run is active (its list stays).

        Checks come from this instance's last pick, else the last pick on any component of the
        same module type (matched by offset), else ``preselect``."""
        if self.state != "idle":
            return False
        key = title if key is None else key
        addresses = {reg["_address"] for reg in registers}
        saved = self.settings["selections"].get(key)
        offsets = self.settings["types"].get(kind) if kind is not None else None
        if saved is None and offsets is not None:
            saved = [reg["_address"] for reg in registers if parse_addr(reg["offset"]) in offsets]
        chosen = (set(saved) if saved is not None else set(preselect)) & addresses
        self._selection_key = key
        self._selection_kind = kind
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
        self._update_count()   # only user edits are remembered, so untouched instances keep following the type
        self._show_selection()
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
        self._show_selection()

    @property
    def shown(self):
        """(name, address) of the chart lanes: the run while active, else the checked registers."""
        return self.monitored if self.state != "idle" or self._shown is None else self._shown

    def _show_selection(self):
        """Idle chart follows the checks; registers of the last run keep their data."""
        if self.state != "idle":
            return
        chosen = self.checked_registers()
        if self.buffer is not None:
            # The buffer owns the column order.  Do not derive it from the
            # mutable register list after a run has stopped; that list can be
            # rebuilt when the user changes the checked rows or component.
            addresses = self.buffer.addresses or tuple(address for _, address in self.monitored)
            runs = {address: i for i, address in enumerate(addresses)}
        else:
            runs = {}
        columns = [runs.get(address) for _, address in chosen]
        offsets = {item.data(Qt.UserRole): item.data(Qt.UserRole + 2) for item in self._items()}
        self._shown = chosen
        self.plot.set_lanes(self.buffer if any(c is not None for c in columns) else None,
                            [name for name, _ in chosen], [offsets[address] for _, address in chosen], columns)
        self._sync_highlight()

    def _remember_selection(self):
        if self._selection_key is not None:
            self.settings["selections"][self._selection_key] = self.checked_addresses()
            if self._selection_kind is not None:
                self.settings["types"][self._selection_kind] = [
                    item.data(Qt.UserRole + 2) for item in self._items()
                    if item.checkState() == Qt.Checked and item.flags() & Qt.ItemIsUserCheckable]
            self.settings_changed.emit(copy.deepcopy(self.settings))

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.Wheel and watched is getattr(self, "plot", None)
                and not event.modifiers() & Qt.ControlModifier):
            # Mark the gesture before the scrollbar paints, so that paint cannot
            # rebuild the live chart on the same turn.
            self.plot._begin_scroll()
            self.plot_scroll.verticalScrollBar().wheelEvent(event)
            return event.isAccepted()
        scroll = getattr(self, "plot_scroll", None)
        if event.type() == QEvent.Resize and scroll is not None and watched in (scroll, scroll.viewport()):
            self._position_axis_footer()
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
        if self.plot.highlight is not None:
            lane = self.plot.lanes()[self.plot.highlight]
            margin = round(lane.height() / 2)
            if self.plot.highlight == len(self.plot.names) - 1:
                margin += self.plot.AXIS_HEIGHT
            self.plot_scroll.ensureVisible(0, round(lane.center().y()), 0, margin)

    def _sync_highlight(self):
        address = self.list.itemDelegate().focus
        addresses = [shown for _, shown in self.shown]
        self.plot.set_highlight(addresses.index(address) if address in addresses else None)

    def _lane_clicked(self, column):
        if column >= len(self.shown):
            return
        address = self.shown[column][1]
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
        if hasattr(self, "start_button"):
            self._interval_tooltip()

    def clear_selection(self):
        if self.state != "idle":
            return False
        with QSignalBlocker(self.list):
            for item in self._items():
                if item.checkState() == Qt.Checked:
                    item.setCheckState(Qt.Unchecked)
        self._update_count()
        self._remember_selection()
        self._show_selection()
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
        self._epoch += 1
        self._runs += 1
        token = self._epoch
        self._stop = stop = threading.Event()
        self.monitored = chosen
        self._shown = None
        offsets = {item.data(Qt.UserRole): item.data(Qt.UserRole + 2) for item in self._items()}
        self.monitored_offsets = [offsets[address] for _, address in chosen]
        self.sampler = ""
        self._last_data = None
        self._shown_pause = 0
        self.interval_ms = self.interval_spin.value()
        self.plot.sample_interval_s = self.interval_ms / 1000
        self.buffer = MonitorBuffer(len(chosen), limit=monitor_sample_limit(len(chosen)),
                                    addresses=[address for _, address in chosen], store_signed=False)
        self.plot.set_series(self.buffer, [name for name, _ in chosen], self.monitored_offsets)
        self._sync_highlight()
        self._launch(session, token, stop)
        return True

    def _launch(self, session, token, stop):
        """Start board sampling of self.monitored into the current buffer."""
        interval = self.interval_ms
        addresses = [address for _, address in self.monitored]
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
        self._set_status(f"正在启动板端采样（{len(addresses)} 个寄存器，间隔 {interval} ms）…")
        self.pool.start(worker)

    def toggle_pause(self):
        if self.state == "running":
            self.pause()
        elif self.state == "paused":
            self.resume()

    def pause(self):
        """End board sampling but keep the run: its data stays and resume() appends to it."""
        if self.state != "running":
            return False
        self._stop.set()   # same epoch: samples already in flight still belong to this run
        self._set_state("paused")
        self._refresh()
        self._set_status(self._summary("已暂停"))
        return True

    def resume(self):
        if self.state != "paused":
            return False
        session = self._session_provider()
        if session is None:
            self._set_status("请先连接 SSH，再继续监视。", "warn")
            return False
        self._epoch += 1
        token = self._epoch
        self._stop = stop = threading.Event()
        self._last_data = None
        self._shown_pause = 0
        if self.buffer is not None:
            self.buffer.mark_resume()
        self._launch(session, token, stop)
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
            addresses = self.buffer.addresses or tuple(address for _, address in self.monitored)
            self.buffer = MonitorBuffer(len(self.monitored), limit=monitor_sample_limit(len(self.monitored)),
                                        addresses=addresses, store_signed=False)
            if self.state == "idle" and self._shown is not None:
                self._show_selection()
            else:
                self.plot.set_series(self.buffer, [name for name, _ in self.monitored], self.monitored_offsets)
                self._sync_highlight()
        self._set_status("已清空。" if self.state == "idle" else
                         self._summary("已暂停" if self.state == "paused" else "监视中"))

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
        if token != self._epoch or self.state == "paused":   # a paused run ends on purpose
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
        paused = state == "paused"
        self.pause_button.setEnabled(state in ("running", "paused"))
        self.pause_button.setText("继续" if paused else "暂停")
        self.pause_button.setIcon(icon("play" if paused else "pause"))
        self.interval_spin.setEnabled(idle)   # a resumed run keeps its period
        # Lock the rows while keeping the view and its scrollbar interactive.
        with QSignalBlocker(self.list):
            for item in self._items():
                flags = item.flags()
                if flags & Qt.ItemIsUserCheckable:
                    item.setFlags(flags | Qt.ItemIsEnabled if idle else flags & ~Qt.ItemIsEnabled)
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
            details.append(f"保留 {len(buffer):,} 点 · 已丢弃 {buffer.dropped:,} 点旧数据，仅导出保留部分")
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
                self.plot.refresh_data()
            if self.state == "paused":   # samples in flight at the pause still count
                self._set_status(self._summary("已暂停"))
            if self.state != "running":
                return
            if paused:
                self._set_status(self._summary(f"数据暂停 {paused} s（网络或板端无响应，恢复后自动继续）"), "warn")
            else:
                self._set_status(self._summary("监视中"),
                                 "warn" if self.buffer.failed or self.buffer.dropped or self._lagging() else "")

    def _set_status(self, text, kind=""):
        self.status.setText(" · ".join(text.splitlines()))
        if kind != self._status_kind:   # a restyle per data frame is costly
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

    def _interval_changed(self, value):
        self.settings["interval_ms"] = value
        self.settings_changed.emit(copy.deepcopy(self.settings))
        self._interval_tooltip()

    def _interval_tooltip(self):
        value = self.interval_spin.value()
        count = len(self.monitored)
        if not count and hasattr(self, "list"):
            count = len(self.checked_registers())
        count = count or 1
        limit = self.buffer.limit if self.buffer is not None else monitor_sample_limit(count)
        self.interval_spin.setToolTip(f"板端采样间隔，默认 {MONITOR_INTERVAL_MS} ms，最快 {MONITOR_MIN_INTERVAL_MS} ms；"
                                      "监视与暂停期间锁定，下次开始监视时生效。")
        self.start_button.setToolTip(f"板端每 {value} ms 采样一次：aarch64 板子上由常驻采样程序一次映射、循环读取；"
                                     "无法使用时改为 devmem 循环，跟不上时按实际速度采样并在状态栏提示。"
                                     f"最多保留约 {limit * value // 1000} s 数据（{limit:,} 点）。")

    def _signed_changed(self, on):
        self.plot.signed = on
        self.settings["signed"] = on
        self.settings_changed.emit(copy.deepcopy(self.settings))
        self.plot._request_paint()

    def _export_request(self):
        buffer = self.buffer
        if buffer is None or not len(buffer):
            self._set_status("还没有可导出的数据。", "warn")
            return None
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "导出监视数据", f"monitor_{datetime.now():%Y%m%d_%H%M%S}.zip", EXPORT_FILTERS)
        if not path:
            return None
        path = export_path(path, selected_filter)
        # The native file dialog processes incoming samples. Capture the current
        # buffer after it closes, then copy once so a worker can export safely.
        buffer = self.buffer
        if buffer is None or not len(buffer):
            self._set_status("还没有可导出的数据。", "warn")
            return None
        signed = self.signed_check.isChecked()
        begin, end = 0, len(buffer)
        if self.plot.export_range is not None:
            first, last = self.plot.export_range
            begin, end = bisect_left(buffer.times, first), bisect_right(buffer.times, last)
        if begin >= end:
            self._set_status("选区内没有可导出的采样数据。", "warn")
            return None
        return dict(path=path, times=buffer.times[begin:end],
                    columns=[buffer.column(index, signed)[begin:end] for index in range(len(self.monitored))],
                    failures=[array("I", (i - begin for i in column[bisect_left(column, begin):bisect_left(column, end)]))
                              for column in buffer.failures],
                    monitored=tuple(self.monitored), count=end - begin, dropped=buffer.dropped, run=self._runs)

    @staticmethod
    def _write_export(request):
        with csv_export(request["path"]) as handle:
            writer = csv.writer(handle)
            writer.writerow(["time_s"] + [f"{name} (0x{address:08X})" for name, address in request["monitored"]])
            pending = [iter(column) for column in request["failures"]]
            next_failure = [next(column, None) for column in pending]
            for index, stamp in enumerate(request["times"]):
                row_values = [f"{stamp:.6f}"]
                for column, values in enumerate(request["columns"]):
                    if index == next_failure[column]:
                        row_values.append("")
                        next_failure[column] = next(pending[column], None)
                    else:
                        row_values.append(str(values[index]))
                writer.writerow(row_values)
        # Queue only completion metadata back to the UI.
        return {key: request[key] for key in ("path", "count", "dropped", "run")}

    def _export_finished(self, result):
        if result["run"] != self._runs:
            return   # a previous run must not replace the new run's status
        note = f"（已丢弃 {result['dropped']:,} 点旧数据）" if result["dropped"] else ""
        self._set_status(f"已导出保留的 {result['count']:,} 点到 {result['path']}{note}",
                         "warn" if result["dropped"] else "")

    def export_csv(self):
        """Synchronous export for callers; the UI uses export_data() in a worker."""
        request = self._export_request()
        if request is None:
            return None
        try:
            result = self._write_export(request)
        except OSError as exc:
            self._set_status(f"导出失败：{exc}", "error")
            return None
        self._export_finished(result)
        return result["path"]

    def export_data(self):
        if self._exporting:
            return
        request = self._export_request()
        if request is None:
            return
        self._exporting = True
        self.export_button.setEnabled(False)
        self.export_button.setText("导出中…")
        self._set_status(f"正在导出 {request['count']:,} 点…")
        worker = Worker(lambda progress: self._write_export(request))
        worker.signals.result.connect(self._export_finished)
        worker.signals.failed.connect(lambda message: self.bridge.export_failed.emit(request["run"], message))
        worker.signals.finished.connect(self._export_done)
        worker.signals.finished.connect(lambda: self._workers.discard(worker))
        self._workers.add(worker)
        self.export_pool.start(worker)

    def _export_failed(self, run, message):
        if run == self._runs:
            self._set_status(f"导出失败：{message}", "error")

    def _export_done(self):
        self._exporting = False
        self.export_button.setEnabled(True)
        self.export_button.setText("导出数据")

    def shutdown(self):
        """Owner is closing: stop sampling and drop late results."""
        self._epoch += 1
        self._stop.set()
        self._set_state("idle")
        # An in-flight CSV/zip export holds the export_pool; drop queued work and
        # stop waiting on it so the dialog can be destroyed without a hang.
        self.export_pool.clear()
        self.export_pool.waitForDone(50)
        self._exporting = False

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)

    def reject(self):
        self.stop()
        super().reject()
