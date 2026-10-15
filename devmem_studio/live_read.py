# -*- coding: utf-8 -*-
"""Live register table: the visible rows stream through the monitor's board-side sampler."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QThreadPool, Signal

from .core import MONITOR_MAX_REGISTERS
from .widgets import Worker

# Reading these changes hardware state: never sampled live.
NO_LIVE_FLAGS = ("write_only", "read_clear", "read_to_clear", "read_side_effect")


class LiveReader(QObject):
    """One sampler per MONITOR_MAX_REGISTERS addresses on its own exec channel (the shell
    stays free for writes); only each run's latest sample reaches the UI."""
    values = Signal(object)    # {address: int | None}
    stopped = Signal(str)      # a run ended by itself (fault, sampler exit, start failure)

    _samples = Signal(int, object)
    _ended = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.key = None
        self._token = 0
        self._stop = threading.Event()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)   # starts queue up; a superseded one returns at once
        self._workers = set()
        self._samples.connect(self._deliver)
        self._ended.connect(self._end)

    def start(self, session, addresses, interval_ms, key):
        self.stop()
        self.key = key
        token = self._token
        stop = self._stop = threading.Event()
        chunks = [addresses[i:i + MONITOR_MAX_REGISTERS] for i in range(0, len(addresses), MONITOR_MAX_REGISTERS)]

        def run(_progress):
            for chunk in chunks:
                def on_samples(samples, chunk=chunk):
                    self._samples.emit(token, dict(zip(chunk, samples[-1][1])))
                if not session.start_monitor(chunk, interval_ms, on_samples,
                                             lambda message: self._ended.emit(token, message), stop):
                    return
        worker = Worker(run)
        worker.signals.failed.connect(lambda message: self._ended.emit(token, f"启动失败：{message}"))
        worker.signals.finished.connect(lambda: self._workers.discard(worker))
        self._workers.add(worker)
        self._pool.start(worker)

    def stop(self):
        self._stop.set()
        self._token += 1   # late samples of the old run are dropped
        self.key = None

    def _deliver(self, token, values):
        if token == self._token:
            self.values.emit(values)

    def _end(self, token, message):
        if token == self._token and message:   # '' = cancelled by stop()
            self.stop()
            self.stopped.emit(message)
