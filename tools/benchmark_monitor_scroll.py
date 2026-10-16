"""Replay wheel input while 32 columns of 200k samples continue to arrive."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QWheelEvent
from PySide6.QtWidgets import QApplication
from devmem_studio.monitor import MonitorBuffer, MonitorDialog, MonitorPlot
from devmem_studio.theme import STYLE
from devmem_studio import monitor

render_lane, paint_event = MonitorPlot._render_lane, MonitorPlot.paintEvent
paint_depth = 0
renders, paints, wheels, steps = [], [], [], []

class TimedPlot(MonitorPlot):
    def _render_lane(self, *arguments, **kwargs):
        start = time.perf_counter()
        result = render_lane(self, *arguments, **kwargs)
        renders.append((paint_depth > 0, (time.perf_counter()-start)*1000))
        return result

    def paintEvent(self, event):
        global paint_depth
        start = time.perf_counter()
        paint_depth += 1
        try:
            return paint_event(self, event)
        finally:
            paints.append((time.perf_counter()-start)*1000)
            paint_depth -= 1

monitor.MonitorPlot = TimedPlot

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
args = parser.parse_args()
app = QApplication([])
for filename in ("msyh.ttc", "consola.ttf"):
    QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / filename))
app.setStyle("Fusion")
app.setFont(QFont("Microsoft YaHei UI", 9))
app.setStyleSheet(STYLE)
dialog = MonitorDialog(lambda: None)
dialog.show()
app.processEvents()
buffer = MonitorBuffer(32, limit=250000)
for begin in range(0, 200000, 1000):
    buffer.extend([(i*.005, tuple((i*1103515245+j*12345) & 0xffffffff for j in range(32)))
                   for i in range(begin, begin+1000)])
dialog.buffer = buffer
dialog.monitored = [(f"R{i}", 0x1000+i*4) for i in range(32)]
plot = dialog.plot
plot.set_series(buffer, [name for name, _ in dialog.monitored])
app.processEvents()
plot.repaint()
app.processEvents()
dialog._set_state("running")
def samples():
    begin = buffer.total
    dialog._samples(dialog._epoch, [(i*.005, tuple((i*1103515245+j*12345) & 0xffffffff for j in range(32)))
                                    for i in range(begin, begin+8)])

timer = QTimer()
timer.setInterval(40)
timer.timeout.connect(samples)
try:
    renders.clear()
    paints.clear()
    timer.start()
    initial = buffer.total
    for delta in [-120]*30 + [120]*30:
        start = time.perf_counter()
        position = QPointF(plot.lanes()[0].center().x(), plot._visible_rect().center().y())
        event = QWheelEvent(position, position, QPoint(), QPoint(0, delta),
                            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        wheel_start = time.perf_counter()
        QApplication.sendEvent(plot, event)
        wheels.append((time.perf_counter()-wheel_start)*1000)
        app.processEvents()
        steps.append((time.perf_counter()-start)*1000)
        time.sleep(max(.001, .016-(time.perf_counter()-start)))
    timer.stop()
    deadline = time.perf_counter()+.3
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(.005)
    result = dict(samples_before=initial, samples_after=buffer.total,
                  synchronous_lane_renders=sum(sync for sync, _ in renders), lane_renders=len(renders),
                  max_wheel_ms=round(max(wheels), 3), max_paint_ms=round(max(paints), 3),
                  max_scroll_step_ms=round(max(steps), 3),
                  p95_scroll_step_ms=round(sorted(steps)[int(len(steps)*.95)], 3),
                  max_lane_render_ms=round(max(ms for _, ms in renders), 3))
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
finally:
    timer.stop()
    monitor.MonitorPlot = MonitorPlot
    dialog.shutdown()
    dialog.close()
    dialog.deleteLater()
    app.processEvents()
