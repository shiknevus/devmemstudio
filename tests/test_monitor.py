"""Register monitor: board sampler script, stream parser, sample buffer and plot."""
import os
import random
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPoint, QPointF, QRectF
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem
from PySide6.QtTest import QTest, QSignalSpy

from devmem_studio.core import (ConfigStore, MonitorParser, monitor_command, monitor_script, resource_path,
                                MONITOR_MIN_INTERVAL_MS, MONITOR_MAX_REGISTERS, REGMON_RESOURCE)
from devmem_studio.monitor import MonitorBuffer, MonitorDialog, MonitorPlot, nice_step, to_signed


def send_plot_wheel(plot, delta, modifiers=Qt.ControlModifier, pixel_delta=0, position=None):
    if position is None:
        position = plot.lanes()[0].center() if isinstance(plot, MonitorPlot) else QPointF(plot.rect().center())
    event = QWheelEvent(position, position, QPoint(0, pixel_delta), QPoint(0, delta),
                        Qt.NoButton, modifiers, Qt.NoScrollPhase, False)
    QApplication.sendEvent(plot, event)
    return event


class SamplerScriptTests(unittest.TestCase):
    @staticmethod
    def run_shell(path, **kwargs):
        shell = shutil.which("sh")
        env = os.environ.copy()
        if os.name == "nt":
            # Use Git's actual shell; its bin launcher can survive a timeout
            # through a child that inherits stdout. Avoid slow Windows PATH scans.
            direct = Path(shell).parent.parent / "usr" / "bin" / "sh.exe"
            if direct.exists():
                shell = str(direct)
            env["PATH"] = "/usr/bin:/bin"
        # MSYS startup on busy Windows hosts can take longer than the sampler
        # itself; the script still exits after its finite number of reads.
        return subprocess.run([shell, str(path)], env=env, timeout=45, **kwargs)

    def test_script_reads_each_address_with_microsecond_period(self):
        script = monitor_script([0xB0100800, 0xB0100804], 5)
        self.assertTrue(script.startswith("P=5000;"))
        self.assertIn("devmem 0xb0100800 || echo x; devmem 0xb0100804 || echo x;", script)
        self.assertIn("usleep $d", script)
        self.assertIn(':/usr/sbin:/sbin:/run/media/sda/bin"; export PATH;', script)
        self.assertLess(script.index("command -v devmem"), script.index("while :"))
        self.assertIn("exit 127", script)
        self.assertIn('echo "@$t" || exit;', script)  # channel closure must end the board loop
        self.assertEqual(MONITOR_MIN_INTERVAL_MS, 5)
        self.assertEqual(MONITOR_MAX_REGISTERS, 8)
        with self.assertRaises(ValueError):
            monitor_script([], 5)
        with self.assertRaises(ValueError):
            monitor_script([0x1_0000_0000], 5)

    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX sh")
    def test_script_runs_and_holds_the_period_under_posix_sh(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sampler.sh"
            # Stop inside the shell instead of killing an infinite loop: on
            # Windows Git sh's child can otherwise retain the capture pipes.
            path.write_text("reads=0; devmem() { reads=$((reads + 1)); [ $reads -le 12 ] || exit 0; echo 0x0000002A; }\n"
                            + monitor_script([0xB0100800, 0xB0100804], 200) + "\n",
                            encoding="utf-8", newline="\n")
            result = self.run_shell(path, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = result.stdout
        parser = MonitorParser(2)
        parser.feed(output)
        samples = parser.take()
        self.assertGreaterEqual(len(samples), 4)
        self.assertTrue(all(values == (0x2A, 0x2A) for _, values in samples))
        period = (samples[-1][0] - samples[0][0]) / (len(samples) - 1)
        self.assertGreater(period, 0.15)   # deadline pacing, not a busy loop
        self.assertLess(period, 0.4)

    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX sh")
    def test_missing_devmem_exits_instead_of_streaming_failed_points(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing-devmem.sh"
            path.write_text("command() { return 1; }\n" + monitor_script([0x1000], 5) + "\n",
                            encoding="utf-8", newline="\n")
            result = self.run_shell(path, capture_output=True)
        self.assertEqual(result.returncode, 127)
        self.assertIn(b"devmem: not found", result.stderr)
        self.assertEqual(result.stdout, b"")

    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX sh")
    def test_resident_sampler_exit_status_decides_the_devmem_handover(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for status, handover in ((127, True), (2, False), (0, False)):
                with self.subTest(status=status):
                    sampler = folder / f"regmon-{status}"
                    if status != 127:   # 127: the board cannot find or run it
                        sampler.write_text(f"#!/bin/sh\necho '#regmon 1'\nexit {status}\n", newline="\n")
                    path = folder / "monitor.sh"
                    path.write_text("reads=0; devmem() { reads=$((reads + 1)); [ $reads -le 2 ] || exit 0; echo 0x2A; }\n"
                                    + monitor_command([0xB0100800], 200, sampler.as_posix()) + "\n",
                                    encoding="utf-8", newline="\n")
                    result = self.run_shell(path, capture_output=True)
                    self.assertEqual(result.returncode, 0 if handover else status, result.stderr)
                    self.assertEqual(b"#shell" in result.stdout, handover)
                    self.assertEqual(result.stdout.count(b"0x2A"), 2 if handover else 0)


class ParserTests(unittest.TestCase):
    def test_backwards_board_clock_stops_instead_of_corrupting_plot_order(self):
        parser = MonitorParser(1)
        with self.assertRaisesRegex(ValueError, '时间倒退'):
            parser.feed(b'@2000000000\n0x1\n@1000000000\n0x2\n')
    def test_board_stamps_failures_and_split_chunks(self):
        parser = MonitorParser(2, clock=lambda: 99.0)
        parser.feed(b"@1700000000123456789\n0x00000001\n0x0000000A\n@17000000001")
        parser.feed(b"33456789\nx\nsh: devmem: oops\n0x2\n")
        self.assertEqual(parser.take(), [(1700000000.1234567, (1, 10)), (1700000000.133457, (None, 2))])
        self.assertEqual(parser.note, "sh: devmem: oops")
        self.assertEqual(parser.take(), [])

    def test_missing_board_clock_falls_back_to_host_time(self):
        parser = MonitorParser(1, clock=lambda: 42.5)
        parser.feed(b"@\n0x7\n@%s%N\n0x8\n")
        self.assertEqual(parser.take(), [(42.5, (7,)), (42.5, (8,))])

    def test_values_before_the_first_stamp_are_ignored(self):
        parser = MonitorParser(1)
        parser.feed(b"0x5\n@1000000000\n0x6\n0x9\n")
        self.assertEqual(parser.take(), [(1.0, (6,))])

    def test_banner_names_the_sampler_without_becoming_the_note(self):
        parser = MonitorParser(1)
        parser.feed(b"#regmon 1\n@5\n0x1\n")
        self.assertEqual((parser.sampler, parser.note, len(parser.take())), ("regmon", "", 1))
        with self.assertRaisesRegex(ValueError, '时间基准'):
            parser.feed(b"regmon: access fault\n#shell\n")


class CommandTests(unittest.TestCase):
    def test_only_an_unexecutable_resident_sampler_hands_over_to_devmem(self):
        addresses = [0xB0119E08, 0xB0119FC0]
        script = monitor_script(addresses, 5)
        self.assertEqual(monitor_command(addresses, 5), "echo '#shell'; " + script)
        self.assertEqual(monitor_command(addresses, 5, "/tmp/regmon"),
                         "/tmp/regmon 5000 0xb0119e08 0xb0119fc0; s=$?; [ $s -eq 126 ] || [ $s -eq 127 ] || exit $s; "
                         "echo '#shell'; " + script)
        with self.assertRaises(ValueError):
            monitor_command([], 5, "/tmp/regmon")

    def test_monitor_rejects_unaligned_addresses_and_invalid_rates(self):
        for addresses, interval in (([0x1001], 5), ([0x1000], 0), ([0x1000], float('nan')),
                                    ([0x1000] * 9, 10), ([0x1000], 60001)):
            with self.subTest(addresses=addresses, interval=interval), self.assertRaises(ValueError):
                monitor_command(addresses, interval)

    def test_bundled_sampler_is_a_small_static_aarch64_executable(self):
        data = resource_path(REGMON_RESOURCE).read_bytes()
        self.assertEqual(data[:5], b"\x7fELF\x02")                          # ELF64
        self.assertEqual(int.from_bytes(data[16:18], "little"), 2)          # ET_EXEC: static, no loader
        self.assertEqual(int.from_bytes(data[18:20], "little"), 183)        # EM_AARCH64
        self.assertLess(len(data), 16384)


class BufferTests(unittest.TestCase):
    def test_cached_extrema_match_raw_and_signed_values_across_drop_boundaries(self):
        rng = random.Random(42)
        buffer = MonitorBuffer(2, limit=350)
        for begin, end in ((0, 300), (300, 400), (400, 700)):
            buffer.extend([(float(i), (rng.randrange(1 << 32), rng.randrange(1 << 32))) for i in range(begin, end)])
            for signed in (False, True):
                for column in range(2):
                    values = buffer.column(column, signed)
                    for _ in range(50):
                        first = rng.randrange(len(buffer))
                        last = rng.randrange(first + 1, len(buffer) + 1)
                        self.assertEqual(buffer.extrema(column, first, last, signed),
                                         (min(values[first:last]), max(values[first:last])))
        self.assertEqual(buffer.dropped, buffer.total - len(buffer))
    def test_relative_time_failures_hold_and_signed_view(self):
        buffer = MonitorBuffer(2)
        buffer.extend([(100.0, (5, 0xFFFFFFFF)), (100.5, (None, 1)), (101.0, (7, None))])
        self.assertEqual(list(buffer.times), [0.0, 0.5, 1.0])
        self.assertEqual(list(buffer.raw[0]), [5, 5, 7])
        self.assertEqual(list(buffer.signed[1]), [-1, 1, 1])
        self.assertEqual([list(column) for column in buffer.failures], [[1], [2]])
        self.assertEqual((buffer.total, buffer.failed), (3, 2))
        self.assertAlmostEqual(buffer.period(), 0.5)
        self.assertEqual(to_signed(0x80000000), -0x80000000)

    def test_limit_drops_the_oldest_quarter_and_reindexes_failures(self):
        buffer = MonitorBuffer(1, limit=8)
        buffer.extend([(float(i), (None if i in (1, 7) else i,)) for i in range(9)])
        self.assertEqual(len(buffer), 6)
        self.assertEqual(list(buffer.times), [3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        self.assertEqual(list(buffer.failures[0]), [4])
        self.assertEqual(buffer.total, 9)


class DialogTests(unittest.TestCase):
    def test_unreadable_registers_are_disabled_and_saved_checks_removed(self):
        self.registers[0]['write_only'] = True
        self.registers[1]['read_clear'] = True
        self.dialog.set_registers('A', self.registers, [0x1000, 0x1004, 0x1008])
        self.assertEqual(self.dialog.checked_addresses(), [0x1008])
        for index in (0, 1):
            self.assertFalse(self.dialog.list.item(index).flags() & Qt.ItemIsUserCheckable)
            self.dialog.list.item(index).setCheckState(Qt.Checked)
        self.assertEqual(self.dialog.checked_addresses(), [0x1008])

    def test_quiet_run_shows_a_pause_and_resumes_on_new_samples(self):
        self.dialog.interval_ms = 10
        self.dialog.sampler = "regmon"
        self.dialog.monitored = [('R0', 0x1000)]
        self.dialog.buffer = MonitorBuffer(1)
        self.dialog._set_state('running')
        token = self.dialog._epoch
        self.dialog._samples(token, [(0.0, (1,)), (0.01, (2,))])
        self.dialog._refresh()
        self.assertTrue(self.dialog.status.text().startswith('监视中'))
        self.assertNotIn('板端常驻采样', self.dialog.status.text())
        self.dialog._last_data -= 5.2   # nothing arrived for 5.2 s
        self.dialog._refresh()
        self.assertTrue(self.dialog.status.text().startswith('数据暂停 5 s'), self.dialog.status.text())
        self.assertEqual(self.dialog.state, 'running')
        self.dialog._samples(token, [(5.2, (3,)), (5.21, (4,))])
        self.dialog._refresh()
        self.assertTrue(self.dialog.status.text().startswith('监视中'))
        self.assertIn('实际 10.00 ms/点', self.dialog.status.text())   # the gap does not read as lag
        self.assertNotIn('跟不上', self.dialog.status.text())
        self.assertNotIn('板端常驻采样', self.dialog.status.text())
        self.dialog.stop()
        self.assertNotIn('板端常驻采样', self.dialog.status.text())

    def test_truncation_is_visible_in_status_and_csv_export_result(self):
        self.dialog.monitored = [('R0', 0x1000)]
        self.dialog.buffer = MonitorBuffer(1, limit=8)
        self.dialog.buffer.extend([(float(i), (i,)) for i in range(9)])
        self.assertIn('已丢弃 3 点', self.dialog._summary('监视中'))
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'monitor.csv')
            with patch('devmem_studio.monitor.QFileDialog.getSaveFileName', return_value=(path, '')):
                self.assertEqual(self.dialog.export_csv(), path)
            self.assertIn('已丢弃 3 点', self.dialog.status.text())
            self.assertEqual(len(Path(path).read_text(encoding='utf-8-sig').splitlines()), 7)
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = MonitorDialog(lambda: None)
        self.registers = [dict(name=f"R{i}", offset=f"0x{i*4:X}", _address=0x1000+i*4) for i in range(3)]

    def tearDown(self):
        self.dialog.shutdown()
        self.dialog.close()

    def show_register_list(self):
        self.dialog.show()
        self.app.processEvents()
        return self.dialog.list

    def test_sidebar_handle_drags_to_widen_list_during_monitoring(self):
        registers = [dict(name=f"R{i} · long_signal_name_for_monitoring", offset=i * 4,
                          _address=0x1000 + i * 4) for i in range(100)]
        self.dialog.set_registers("A", registers, [0x1000, 0x1004])
        view = self.show_register_list()
        splitter = self.dialog.splitter
        side = splitter.widget(0)
        handle = splitter.handle(1)

        def drag_handle(distance):
            center = handle.rect().center()
            target = center + QPoint(distance, 0)
            QTest.mousePress(handle, Qt.LeftButton, pos=center)
            QApplication.sendEvent(handle, QMouseEvent(QEvent.MouseMove, QPointF(target),
                                                       QPointF(handle.mapToGlobal(target)),
                                                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
            QTest.mouseRelease(handle, Qt.LeftButton, pos=handle.rect().center())
            self.app.processEvents()

        for state in ("idle", "starting", "running"):
            with self.subTest(state=state):
                self.dialog._set_state(state)
                previous_width = view.width()
                previous_plot_width = self.dialog.plot.width()
                drag_handle(40)
                self.assertGreater(view.width(), previous_width)
                self.assertLess(self.dialog.plot.width(), previous_plot_width)
                self.assertEqual(self.dialog.checked_addresses(), [0x1000, 0x1004])
                self.assertEqual(self.dialog.search.width(), view.width())
                scrollbar = view.verticalScrollBar()
                scrollbar.setValue(0)
                send_plot_wheel(view.viewport(), -120, Qt.NoModifier)
                self.assertGreater(scrollbar.value(), 0)
        self.dialog.stop()
        for width in (1600, 1000, 1800):
            with self.subTest(window_width=width):
                self.dialog.resize(width, 680)
                self.app.processEvents()
                self.assertLessEqual(side.width(), self.dialog.width() // 2)
                drag_handle(10000)
                self.assertLessEqual(side.width(), self.dialog.width() // 2)
                if width >= 1600:
                    self.assertEqual(side.width(), self.dialog.width() // 2)
        drag_handle(-1000)
        self.assertGreaterEqual(side.width(), side.minimumWidth())
        self.assertGreaterEqual(self.dialog.plot.width(), self.dialog.plot.minimumWidth())
        self.assertGreater(view.width(), 0)
        self.dialog.clear_selection_button.click()
        self.assertEqual(self.dialog.checked_addresses(), [])

    def test_row_text_blank_area_and_checkbox_each_toggle_once(self):
        self.dialog.set_registers("A", self.registers)
        view = self.show_register_list()
        item = view.item(0)
        rect = view.visualItemRect(item)
        option = QStyleOptionViewItem()
        view.itemDelegate().initStyleOption(option, view.indexFromItem(item))
        option.rect = rect
        checkbox = view.style().subElementRect(QStyle.SE_ItemViewItemCheckIndicator, option, view)
        self.assertFalse(checkbox.isEmpty())
        changes = QSignalSpy(self.dialog.settings_changed)
        for position in (QPoint(checkbox.right() + 10, rect.center().y()),
                         QPoint(rect.right() - 5, rect.center().y()), checkbox.center()):
            with self.subTest(position=position):
                before = changes.count()
                QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=position)
                self.assertEqual(self.dialog.checked_addresses(), [0x1000])
                self.assertEqual(self.dialog.settings["selections"]["A"], [0x1000])
                self.assertEqual(self.dialog.count_label.text(), "已选 1 / 8")
                QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=position)
                self.assertEqual(self.dialog.checked_addresses(), [])
                self.assertEqual(self.dialog.settings["selections"]["A"], [])
                self.assertEqual(changes.count(), before + 2)

    def test_row_click_preserves_selection_limit(self):
        registers = [dict(name=f"R{i}", offset=i * 4, _address=0x1000 + i * 4) for i in range(9)]
        chosen = [reg["_address"] for reg in registers[:8]]
        self.dialog.set_registers("A", registers, chosen)
        view = self.show_register_list()
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(view.item(8)).center())
        self.assertEqual(self.dialog.checked_addresses(), chosen)
        self.assertEqual(self.dialog.settings["selections"]["A"], chosen)
        self.assertIn("最多同时监视 8 个", self.dialog.status.text())
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(view.item(0)).center())
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(view.item(8)).center())
        self.assertEqual(self.dialog.checked_addresses(), chosen[1:] + [0x1020])

    def test_filtered_row_click_and_right_click(self):
        self.dialog.set_registers("A", self.registers)
        view = self.show_register_list()
        self.dialog.search.setText("R2")
        self.app.processEvents()
        position = view.visualItemRect(view.item(2)).center()
        QTest.mouseClick(view.viewport(), Qt.RightButton, pos=position)
        self.assertEqual(self.dialog.checked_addresses(), [])
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=position)
        self.assertEqual(self.dialog.checked_addresses(), [0x1008])

    def test_cancel_selection_clears_hidden_rows_and_remembers_empty_selection(self):
        self.dialog.set_registers("A", self.registers, [0x1000, 0x1004, 0x1008])
        view = self.show_register_list()
        self.dialog.search.setText("R2")
        self.app.processEvents()
        changes = QSignalSpy(self.dialog.settings_changed)
        control = self.dialog.clear_selection_button
        self.assertGreater(control.geometry().left(), self.dialog.count_label.geometry().right())
        QTest.mouseClick(control, Qt.LeftButton)
        self.assertTrue(all(view.item(i).checkState() == Qt.Unchecked for i in range(view.count())))
        self.assertEqual(self.dialog.count_label.text(), "已选 0 / 8")
        self.assertEqual(self.dialog.settings["selections"]["A"], [])
        self.assertEqual(changes.count(), 1)
        self.assertFalse(control.isEnabled())
        self.dialog.search.clear()
        self.dialog.set_registers("A", self.registers, [0x1000])
        self.assertEqual(self.dialog.checked_addresses(), [])
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(view.item(0)).center())
        self.assertTrue(control.isEnabled())
        for state in ("starting", "running"):
            self.dialog._set_state(state)
            self.assertFalse(control.isEnabled())
            QTest.mouseClick(control, Qt.LeftButton)
            self.assertFalse(self.dialog.clear_selection())
            self.assertEqual(self.dialog.checked_addresses(), [0x1000])
        self.dialog.stop()
        self.assertTrue(control.isEnabled())
        QTest.mouseClick(control, Qt.LeftButton)
        self.assertEqual(self.dialog.checked_addresses(), [])

    def test_status_bar_stays_on_one_line_and_dot_follows_state(self):
        self.dialog.buffer = MonitorBuffer(1, limit=8)
        self.dialog.buffer.extend([(i * 0.005, (None if i == 4 else i,)) for i in range(20)])
        self.dialog.sampler = "shell"
        self.dialog._set_status(self.dialog._summary("监视中"))
        self.dialog.show()
        footer, status = self.dialog.footer, self.dialog.status
        for width in (900, 1280, 1120):
            self.dialog.resize(width, 680)
            self.app.processEvents()
            self.assertEqual(status.alignment(), Qt.AlignLeft | Qt.AlignVCenter)
            self.assertFalse(status.wordWrap())
            self.assertNotIn("\n", status.text())
            self.assertEqual(status.height(), 20)
            self.assertEqual(status.toolTip(), status.text())
            left = footer.mapTo(self.dialog, QPoint()).x()
            self.assertEqual(left, self.dialog.splitter.mapTo(self.dialog, QPoint()).x())
            self.assertEqual(footer.width(), self.dialog.splitter.width())
            self.assertGreater(status.mapTo(self.dialog, QPoint()).x(),
                               self.dialog.state_dot.mapTo(self.dialog, QPoint()).x())
            self.assertIn("已采 20 点", status.text())
        self.dialog._set_status("read failed\nconnection closed", "error")
        self.assertEqual(status.text(), "read failed · connection closed")
        self.dialog._set_status(self.dialog._summary("监视中"))

        def dot():
            return self.dialog.state_dot.styleSheet()
        self.assertIn("#A3B1BF", dot())
        self.dialog._set_state("starting")
        self.assertIn("#2463DC", dot())
        self.dialog._set_state("running")
        self.assertIn("#2E9D5B", dot())
        self.dialog._set_status("lagging", "warn")
        self.assertIn("#D59A2E", dot())
        self.dialog.stop()
        self.assertIn("#A3B1BF", dot())
        self.assertIn("已停止", status.text())
        self.dialog._set_status("boom", "error")
        self.assertIn("#C84B4B", dot())

    def test_monitor_controls_stay_grouped_and_do_not_overlap_in_narrow_windows(self):
        self.dialog.set_registers("A", self.registers, [0x1000])
        self.dialog.buffer = MonitorBuffer(1)
        self.dialog.buffer.extend([(i * .005, (i,)) for i in range(201)])
        self.dialog.plot.set_series(self.dialog.buffer, ["R0"], [0x800])
        self.dialog.plot._set_marker(0, .25)
        self.dialog.plot._set_marker(1, .75)
        self.dialog._set_status(self.dialog._summary("监视中"))
        self.dialog.show()

        def rect(control):
            return QRectF(control.mapTo(self.dialog, QPoint()), control.size())

        for width in (1120, 760, 1440):
            with self.subTest(width=width):
                self.dialog.resize(width, 780)
                self.app.processEvents()
                self.dialog.splitter.setSizes([width // 2, width])
                self.app.processEvents()
                d = self.dialog
                widths = {control.width() for control in (d.interval_spin, d.start_button, d.clear_button, d.export_button)}
                self.assertEqual(widths, {110})
                # header: run controls on the title row, right-aligned with the chart
                header_y = rect(d.component_label).center().y()
                for control in (d.interval_spin, d.start_button):
                    self.assertLessEqual(abs(rect(control).center().y() - header_y), 1)
                self.assertGreater(rect(d.interval_spin).left(), rect(d.component_label).right())
                self.assertLess(rect(d.start_button).bottom(), rect(d.search).top())
                self.assertLessEqual(abs(rect(d.start_button).right() - rect(d.plot).right()), 1)
                # toolbar: view options left, data actions right, level with the search box
                toolbar = (d.zoom_button, d.zoom_label, d.signed_check, d.clear_button, d.export_button)
                for control in toolbar:
                    self.assertLessEqual(abs(rect(control).center().y() - rect(d.search).center().y()), 1)
                for first, second in zip(toolbar, toolbar[1:]):
                    self.assertLess(rect(first).right(), rect(second).left())
                self.assertLessEqual(abs(rect(d.export_button).right() - rect(d.plot).right()), 1)
                self.assertGreater(rect(d.plot).top(), rect(d.zoom_button).bottom())
                # markers under the chart, status bar at the bottom
                self.assertGreater(rect(d.marker_label).top(), rect(d.plot).bottom())
                self.assertLessEqual(abs(rect(d.clear_markers_button).center().y() - rect(d.marker_label).center().y()), 1)
                self.assertLess(rect(d.marker_label).right(), rect(d.clear_markers_button).left())
                self.assertLessEqual(abs(rect(d.clear_markers_button).right() - rect(d.plot).right()), 1)
                self.assertGreater(rect(d.footer).top(), rect(d.marker_label).bottom())
                self.assertGreater(rect(d.footer).top(), rect(d.clear_selection_button).bottom())
                self.assertLessEqual(d.splitter.widget(0).width(), d.width() // 2)
                self.assertIn("Δt: 0.500000 s", self.dialog.marker_label.text())
                self.assertIn("已采 201 点", self.dialog.status.text())

        for state in ("starting", "running", "idle"):
            self.dialog._set_state(state)
            self.app.processEvents()
            widths = {control.width() for control in (self.dialog.interval_spin, self.dialog.start_button,
                                                       self.dialog.clear_button, self.dialog.export_button)}
            self.assertEqual(widths, {110})

        registers = [dict(name=f"R{i}", offset=0x800 + i * 4, _address=0x1000 + i * 4) for i in range(8)]
        self.dialog.set_registers("eight", registers, [reg["_address"] for reg in registers])
        buffer = MonitorBuffer(8)
        buffer.extend([(i * .005, tuple(i + column for column in range(8))) for i in range(201)])
        self.dialog.buffer = buffer
        self.dialog.plot.set_series(buffer, [reg["name"] for reg in registers], [reg["offset"] for reg in registers])
        self.dialog._set_status(self.dialog._summary("监视中"))
        for width in (1120, 760):
            self.dialog.resize(width, 680)
            for _ in range(3):
                self.app.processEvents()
            self.assertGreater(rect(self.dialog.footer).top(), rect(self.dialog.plot).bottom())
            self.assertLess(rect(self.dialog.footer).bottom(), self.dialog.height())
            self.assertTrue(all(lane.height() >= 52 for lane in self.dialog.plot.lanes()))

    def test_unreadable_and_running_rows_cannot_be_toggled_by_click(self):
        self.registers[0]["write_only"] = True
        self.registers[1]["read_clear"] = True
        self.dialog.set_registers("A", self.registers)
        view = self.show_register_list()
        for index in (0, 1):
            QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(view.item(index)).center())
            self.assertEqual(view.item(index).checkState(), Qt.Unchecked)
        self.dialog._set_state("running")
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(view.item(2)).center())
        self.assertEqual(self.dialog.checked_addresses(), [])

    def test_row_click_highlights_its_lane_and_lane_label_focuses_the_row(self):
        self.dialog.set_registers("A", self.registers, [0x1000, 0x1008])
        view = self.show_register_list()
        self.dialog.monitored = self.dialog.checked_registers()
        self.dialog.buffer = MonitorBuffer(2)
        self.dialog.buffer.extend([(i * .01, (i, -i)) for i in range(50)])
        self.dialog.plot.set_series(self.dialog.buffer, ["R0", "R2"], [0, 8])
        self.dialog._set_state("running")
        plot, delegate = self.dialog.plot, view.itemDelegate()
        self.assertIsNone(plot.highlight)
        for row, lane in ((2, 1), (1, None), (0, 0)):
            QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(view.item(row)).center())
            self.assertEqual(delegate.focus, 0x1000 + row * 4)
            self.assertEqual(plot.highlight, lane)
            plot.grab()
        self.assertEqual(self.dialog.checked_addresses(), [0x1000, 0x1008])   # focusing never toggles
        lane = plot.lanes()[1]
        QTest.mouseClick(plot, Qt.LeftButton, pos=QPoint(40, int(lane.center().y())))
        self.assertEqual((delegate.focus, plot.highlight), (0x1008, 1))
        self.assertEqual(plot.markers, [None, None])
        self.dialog.clear()
        self.assertEqual(plot.highlight, 1)
        self.dialog.stop()
        view.setCurrentRow(0)
        self.assertEqual((delegate.focus, plot.highlight), (0x1000, 0))

    def test_active_register_list_scrolls_without_changing_checks(self):
        registers = [dict(name=f"R{i}", offset=i * 4, _address=0x1000 + i * 4) for i in range(100)]
        registers[1]["write_only"] = True
        self.dialog.set_registers("A", registers, [0x1000])
        view = self.show_register_list()
        scrollbar = view.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        changes = QSignalSpy(self.dialog.settings_changed)
        for state in ("starting", "running"):
            with self.subTest(state=state):
                self.dialog._set_state(state)
                self.assertTrue(view.isEnabled())
                self.assertTrue(scrollbar.isEnabled())
                scrollbar.setValue(0)
                send_plot_wheel(view.viewport(), -120, Qt.NoModifier)
                self.assertGreater(scrollbar.value(), 0)
                send_plot_wheel(view.viewport(), 120, Qt.NoModifier)
                self.assertEqual(scrollbar.value(), 0)
                QTest.keyClick(scrollbar, Qt.Key_End)
                self.assertEqual(scrollbar.value(), scrollbar.maximum())
                QTest.keyClick(scrollbar, Qt.Key_Home)
                self.assertEqual(scrollbar.value(), 0)
                item = view.item(0)
                rect = view.visualItemRect(item)
                option = QStyleOptionViewItem()
                view.itemDelegate().initStyleOption(option, view.indexFromItem(item))
                option.rect = rect
                checkbox = view.style().subElementRect(QStyle.SE_ItemViewItemCheckIndicator, option, view)
                for position in (rect.center(), checkbox.center()):
                    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=position)
                view.setCurrentRow(0)
                QTest.keyClick(view, Qt.Key_Space)
                self.assertEqual(self.dialog.checked_addresses(), [0x1000])
                self.assertEqual(self.dialog.settings["selections"]["A"], [0x1000])
                self.assertEqual(self.dialog.count_label.text(), "已选 1 / 8")
                self.assertEqual(changes.count(), 0)
        self.dialog.stop()
        self.assertFalse(view.item(1).flags() & Qt.ItemIsEnabled)
        self.assertEqual(changes.count(), 0)
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(view.item(0)).center())
        self.assertEqual(self.dialog.checked_addresses(), [])
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.visualItemRect(view.item(2)).center())
        self.assertEqual(self.dialog.checked_addresses(), [0x1008])

    def test_ctrl_wheel_updates_indicator_and_zoom_button_restores_global_view(self):
        self.dialog.show()
        self.app.processEvents()
        plot = self.dialog.plot
        buffer = MonitorBuffer(1)
        buffer.extend([(float(i), (i,)) for i in range(101)])
        plot.set_series(buffer, ["R0"])
        original = plot.time_range()
        changes = QSignalSpy(self.dialog.settings_changed)
        send_plot_wheel(plot, 120)
        self.assertLess(plot.time_range()[1] - plot.time_range()[0], original[1] - original[0])
        self.assertEqual(self.dialog.zoom_label.text(), "125%")
        zoomed_range = plot.time_range()
        QTest.mouseClick(self.dialog.zoom_label, Qt.LeftButton)
        self.assertEqual(plot.time_range(), zoomed_range)
        self.assertEqual(self.dialog.zoom_label.text(), "125%")
        send_plot_wheel(plot, -120)
        self.assertEqual(plot.time_range(), original)
        self.assertEqual(self.dialog.zoom_label.text(), "100%")
        send_plot_wheel(plot, -120)
        self.assertEqual(self.dialog.zoom_label.text(), "80%")
        QTest.mouseClick(self.dialog.zoom_button, Qt.LeftButton)
        self.assertEqual(plot.time_range(), original)
        self.assertEqual(self.dialog.zoom_label.text(), "100%")
        self.assertEqual(changes.count(), 0)
        self.assertEqual(original, (0.0, 100.0))

    def test_zoom_limits_reset_and_live_monitoring(self):
        self.dialog.show()
        self.app.processEvents()
        plot = self.dialog.plot
        send_plot_wheel(plot, 6000)
        self.assertEqual(plot.zoom_factor, plot.MAX_ZOOM)
        self.assertEqual(self.dialog.zoom_label.text(), "6400%")
        send_plot_wheel(plot, -6000)
        self.assertEqual(plot.zoom_factor, plot.MIN_ZOOM)
        self.assertEqual(self.dialog.zoom_label.text(), "25%")
        QTest.mouseClick(self.dialog.zoom_button, Qt.LeftButton)
        self.assertEqual(self.dialog.zoom_label.text(), "100%")
        self.assertEqual(plot.time_range(), (0.0, 1.0))
        self.dialog._set_state("running")
        send_plot_wheel(plot, 120)
        self.assertEqual(self.dialog.zoom_label.text(), "125%")
        self.assertEqual(self.dialog.state, "running")
        buffer = MonitorBuffer(1)
        buffer.extend([(float(i), (i,)) for i in range(101)])
        self.dialog.buffer = buffer
        plot.set_series(buffer, ["R0"])
        self.dialog._samples(self.dialog._epoch, [(101.0, (101,))])
        QTest.mouseClick(self.dialog.zoom_button, Qt.LeftButton)
        self.assertEqual(plot.time_range(), (0.0, 101.0))
        self.assertEqual(self.dialog.zoom_label.text(), "100%")
        self.assertEqual(self.dialog.state, "running")
        self.assertEqual(len(buffer), 102)

    def test_legacy_time_window_setting_does_not_limit_chart(self):
        dialog = MonitorDialog(lambda: None, settings={"window_s": 1, "signed": True,
                                                      "selections": {"A": [0x1000]}})
        try:
            buffer = MonitorBuffer(1)
            buffer.extend([(float(i), (i,)) for i in range(101)])
            dialog.plot.set_series(buffer, ["R0"])
            self.assertEqual(dialog.plot.time_range(), (0.0, 100.0))
            self.assertNotIn("window_s", dialog.settings)
            self.assertFalse(hasattr(dialog, "window_combo"))
            self.assertTrue(dialog.signed_check.isChecked())
            dialog.set_registers("A", self.registers)
            self.assertEqual(dialog.checked_addresses(), [0x1000])
        finally:
            dialog.shutdown()
            dialog.close()

    def test_marker_readout_difference_clear_and_live_data(self):
        self.dialog.show()
        self.app.processEvents()
        self.dialog.monitored = [("R0", 0x1000)]
        self.dialog.monitored_offsets = [0x800]
        self.dialog.buffer = MonitorBuffer(1)
        self.dialog.buffer.extend([(i * 0.005, (1,)) for i in range(101)])
        plot = self.dialog.plot
        plot.set_series(self.dialog.buffer, ["R0"], [0x800])
        self.dialog._set_state("running")
        QTest.mouseClick(plot, Qt.LeftButton, pos=PlotTests.mark_position(plot, 0.1))
        QTest.mouseClick(plot, Qt.LeftButton, pos=PlotTests.mark_position(plot, 0.3))
        self.assertEqual(self.dialog.marker_label.text(), "A: 0.100000 s   B: 0.300000 s   Δt: 0.200000 s")
        self.dialog._samples(self.dialog._epoch, [(0.505, (2,))])
        QTest.mouseClick(self.dialog.zoom_button, Qt.LeftButton)
        self.assertEqual(plot.markers, [0.1, 0.3])
        self.dialog.stop()
        self.assertEqual(plot.markers, [0.1, 0.3])
        QTest.mouseClick(self.dialog.clear_markers_button, Qt.LeftButton)
        self.assertEqual(self.dialog.marker_label.text(), "A: —   B: —   Δt: —")
        self.assertEqual(len(self.dialog.buffer), 102)
        QTest.mouseClick(plot, Qt.LeftButton, pos=PlotTests.mark_position(plot, 0.1))
        QTest.mouseClick(plot, Qt.LeftButton, pos=PlotTests.mark_position(plot, 0.105))
        self.assertAlmostEqual(plot.markers[0], 0.1)
        self.assertAlmostEqual(plot.markers[1], 0.105)
        self.assertIn("Δt: 0.005000 s", self.dialog.marker_label.text())
        self.dialog.clear_markers_button.click()
        QTest.mouseClick(plot, Qt.LeftButton, pos=PlotTests.mark_position(plot, 0.3))
        QTest.mouseClick(plot, Qt.LeftButton, pos=PlotTests.mark_position(plot, 0.1))
        self.assertIn("Δt: 0.200000 s", self.dialog.marker_label.text())
        self.dialog.clear()
        self.assertEqual(plot.markers, [None, None])
        self.assertEqual(self.dialog.marker_label.text(), "A: —   B: —   Δt: —")

    def test_preferences_and_component_selections_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConfigStore(Path(folder) / "settings.json")
            cfg = store.load()
            self.dialog.settings_changed.connect(lambda settings: cfg.update(monitor_settings=settings))
            self.dialog.interval_changed.connect(lambda interval: cfg.update(monitor_interval_ms=interval))
            self.dialog.set_registers("Component A", self.registers, [0x1000], key="top:A")
            self.dialog.list.item(2).setCheckState(Qt.Checked)
            self.dialog.set_registers("Component B", self.registers, [0x1004], key="top:B")
            self.dialog.list.item(1).setCheckState(Qt.Unchecked)  # remember an intentionally empty selection
            self.dialog.interval_spin.setValue(25)
            self.dialog.signed_check.setChecked(True)
            store.save(cfg)
            saved = store.load()
            restored = MonitorDialog(lambda: None, saved['monitor_interval_ms'], settings=saved['monitor_settings'])
            try:
                self.assertEqual(restored.interval_spin.value(), 25)
                self.assertNotIn("window_s", restored.settings)
                self.assertTrue(restored.signed_check.isChecked())
                self.assertTrue(restored.plot.signed)
                restored.set_registers("Renamed A", self.registers, [0x1004], key="top:A")
                self.assertEqual(restored.checked_addresses(), [0x1000, 0x1008])
                restored.set_registers("B", self.registers, [0x1004], key="top:B")
                self.assertEqual(restored.checked_addresses(), [])
                restored.set_registers("New project A", self.registers, [0x1004], key="other-top:A")
                self.assertEqual(restored.checked_addresses(), [0x1004])
            finally:
                restored.shutdown()
                restored.close()

    def test_stop_then_clear_ignores_queued_samples_and_errors(self):
        self.dialog.monitored = [("R0", 0x1000)]
        self.dialog.buffer = MonitorBuffer(1)
        self.dialog._epoch = 7
        self.dialog._set_state("running")
        self.dialog._samples(7, [(100.0, (1,))])
        self.dialog.stop()
        self.dialog.clear()
        status = self.dialog.status.text()
        self.dialog._samples(7, [(100.005, (2,))])
        self.dialog._stopped(7, "late error")
        self.assertEqual(len(self.dialog.buffer), 0)
        self.assertEqual(self.dialog.status.text(), status)


class PlotTests(unittest.TestCase):
    def test_ctrl_wheel_preserves_time_under_mouse_at_different_positions(self):
        plot = self.marker_plot()
        try:
            lane = plot.lanes()[0]
            for fraction in (0.0, 0.2, 0.7, 1.0):
                with self.subTest(fraction=fraction):
                    plot.set_zoom(1.0)
                    begin, end = plot.time_range()
                    anchor = begin + fraction * (end - begin)
                    position = QPointF(lane.left() + fraction * lane.width(), lane.center().y())
                    send_plot_wheel(plot, 120, position=position)
                    new_begin, new_end = plot.time_range()
                    self.assertAlmostEqual(new_begin + fraction * (new_end - new_begin), anchor)
                    self.assertAlmostEqual(new_end - new_begin, (end - begin) / 1.25)
                    send_plot_wheel(plot, -120, position=position)
                    restored_begin, restored_end = plot.time_range()
                    self.assertAlmostEqual(restored_begin, begin)
                    self.assertAlmostEqual(restored_end, end)
            position = QPointF(lane.left() + 0.7 * lane.width(), lane.center().y())
            send_plot_wheel(plot, 0, pixel_delta=60, position=position)
            begin, end = plot.time_range()
            self.assertAlmostEqual(begin + 0.7 * (end - begin), 7.0)
        finally:
            plot.close()

    def test_anchored_view_stays_fixed_on_new_samples_and_zoom_resets_at_same_scale(self):
        plot = self.marker_plot()
        try:
            lane = plot.lanes()[0]
            send_plot_wheel(plot, 120, position=QPointF(lane.left() + 0.2 * lane.width(), lane.center().y()))
            zoomed = plot.time_range()
            plot.buffer.extend([(11.0, (1, 2))])
            self.assertEqual(plot.time_range(), zoomed)
            send_plot_wheel(plot, -120, position=QPointF(lane.left() + 0.8 * lane.width(), lane.center().y()))
            self.assertEqual(plot.zoom_factor, 1.0)
            self.assertNotEqual(plot.time_range(), (0.0, 11.0))
            plot.set_zoom(1.0)
            self.assertEqual(plot.time_range(), (0.0, 11.0))
            send_plot_wheel(plot, 120)
            plot.set_series(MonitorBuffer(1), ["new"], [0x804])
            self.assertEqual(plot.time_range(), (0.0, 0.8))
        finally:
            plot.close()

    def test_anchored_view_excludes_offscreen_spikes_and_paints_blank_ranges(self):
        plot = MonitorPlot()
        plot.resize(900, 320)
        buffer = MonitorBuffer(1)
        buffer.extend([(float(i), (999 if i == 80 else 1,)) for i in range(101)])
        plot.set_series(buffer, ["R0"], [0x800])
        lane = plot.lanes()[0]
        position = QPointF(lane.left() + 0.2 * lane.width(), lane.center().y())
        send_plot_wheel(plot, 600, position=position)
        self.assertLess(plot.time_range()[1], 80.0)
        self.assertFalse(plot.grab().isNull())
        self.assertEqual(plot._trace_cache[0][:2], (1.0, 1.0))
        plot.set_zoom(0.25)
        position = QPointF(lane.left() + 0.75 * lane.width(), lane.center().y())
        send_plot_wheel(plot, 6000, position=position)
        self.assertGreater(plot.time_range()[0], 100.0)
        self.assertFalse(plot.grab().isNull())
        self.assertEqual(plot._trace_cache, {})
        send_plot_wheel(plot, 120, position=QPointF(50, 50))
        self.assertEqual(buffer.total, 101)

    def marker_plot(self):
        plot = MonitorPlot()
        plot.resize(900, 320)
        buffer = MonitorBuffer(2)
        buffer.extend([(float(i), (1, 2)) for i in range(11)])
        plot.set_series(buffer, ["R0", "R1"], [0x800, 0x804])
        plot.show()
        self.app.processEvents()
        return plot

    @staticmethod
    def mark_position(plot, stamp, lane=0):
        rect = plot.lanes()[lane]
        begin, end = plot.time_range()
        return QPoint(round(rect.left() + (stamp - begin) / (end - begin) * rect.width()),
                      round(rect.center().y()))

    def test_clicks_place_two_sample_markers_and_zoom_preserves_times(self):
        plot = self.marker_plot()
        try:
            changes = QSignalSpy(plot.markers_changed)
            QTest.mouseClick(plot, Qt.LeftButton, pos=self.mark_position(plot, 2))
            QTest.mouseClick(plot, Qt.LeftButton, pos=self.mark_position(plot, 7, lane=1))
            self.assertEqual(plot.markers, [2.0, 7.0])
            self.assertEqual(changes.count(), 2)
            self.assertFalse(plot.grab().isNull())
            send_plot_wheel(plot, 120)
            self.assertEqual(plot.markers, [2.0, 7.0])
            QTest.mouseClick(plot, Qt.LeftButton, pos=self.mark_position(plot, 4))
            self.assertEqual(plot.markers, [4.0, 7.0])
            self.assertEqual(plot.buffer.total, 11)
        finally:
            plot.close()

    def test_marker_drag_and_right_click_clear(self):
        plot = self.marker_plot()
        try:
            QTest.mouseClick(plot, Qt.LeftButton, pos=self.mark_position(plot, 2))
            QTest.mouseClick(plot, Qt.LeftButton, pos=self.mark_position(plot, 7))
            QTest.mousePress(plot, Qt.LeftButton, pos=self.mark_position(plot, 2))
            target = self.mark_position(plot, 5)
            QApplication.sendEvent(plot, QMouseEvent(QEvent.MouseMove, QPointF(target), QPointF(target),
                                                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
            QTest.mouseRelease(plot, Qt.LeftButton, pos=target)
            self.assertEqual(plot.markers, [5.0, 7.0])
            QTest.mouseClick(plot, Qt.RightButton, pos=self.mark_position(plot, 6))
            self.assertEqual(plot.markers, [None, None])
            QTest.mouseClick(plot, Qt.LeftButton, pos=self.mark_position(plot, 8))
            self.assertEqual(plot.markers, [8.0, None])
        finally:
            plot.close()

    def test_marks_ignore_gutter_empty_plot_and_blank_time_range(self):
        plot = self.marker_plot()
        try:
            QTest.mouseClick(plot, Qt.LeftButton, pos=QPoint(50, 50))
            self.assertEqual(plot.markers, [None, None])
            plot.set_zoom(0.5)
            QTest.mouseClick(plot, Qt.LeftButton, pos=self.mark_position(plot, 15))
            self.assertEqual(plot.markers, [None, None])
            QTest.mouseClick(plot, Qt.LeftButton, pos=self.mark_position(plot, 3))
            self.assertEqual(plot.markers, [3.0, None])
            plot.set_series(MonitorBuffer(1), ["R0"], [0x800])
            self.assertEqual(plot.markers, [None, None])
            QTest.mouseClick(plot, Qt.LeftButton, pos=plot.lanes()[0].center().toPoint())
            self.assertEqual(plot.markers, [None, None])
        finally:
            plot.close()

    def test_headers_show_name_offset_and_latest_value_for_all_eight_series(self):
        plot = MonitorPlot()
        buffer = MonitorBuffer(8)
        names = [f"R{i}" for i in range(8)]
        offsets = [0x800 + i * 4 for i in range(8)]
        plot.set_series(buffer, names, offsets)
        self.assertEqual(plot.header_texts(0), ("R0", "+0x800", "—"))
        buffer.extend([(0.0, tuple(range(8))), (0.005, (0xFFFFFFFF,) + tuple(range(1, 8)))])
        self.assertEqual(plot.header_texts(0), ("R0", "+0x800", "0xFFFFFFFF · 4294967295"))
        self.assertEqual(plot.header_texts(7), ("R7", "+0x81C", "0x00000007 · 7"))
        plot.signed = True
        self.assertEqual(plot.header_texts(0)[2], "0xFFFFFFFF · -1")
        plot.resize(900, plot.minimumHeight())
        self.assertTrue(all(lane.height() >= 52 for lane in plot.lanes()))
        self.assertFalse(plot.grab().isNull())

    def test_ctrl_wheel_zooms_time_axis_and_plain_wheel_is_ignored(self):
        plot = MonitorPlot()
        buffer = MonitorBuffer(1)
        buffer.extend([(float(i), (i,)) for i in range(101)])
        plot.set_series(buffer, ["R0"])
        original = plot.time_range()
        self.assertFalse(send_plot_wheel(plot, 120, Qt.NoModifier).isAccepted())
        self.assertEqual(plot.time_range(), original)
        self.assertTrue(send_plot_wheel(plot, 120).isAccepted())
        self.assertEqual(plot.time_range(), (10.0, 90.0))
        self.assertFalse(plot.grab().isNull())
        send_plot_wheel(plot, -120)
        self.assertEqual(plot.time_range(), original)
        send_plot_wheel(plot, 0, pixel_delta=60)
        self.assertEqual(plot.time_range(), (10.0, 90.0))
        self.assertEqual(buffer.total, 101)
        self.assertEqual(list(buffer.raw[0]), list(range(101)))

    def test_zoom_handles_empty_and_dropped_data(self):
        plot = MonitorPlot()
        send_plot_wheel(plot, 120)
        begin, end = plot.time_range()
        self.assertGreaterEqual(begin, 0)
        self.assertGreater(end, begin)
        self.assertFalse(plot.grab().isNull())
        buffer = MonitorBuffer(1, limit=8)
        buffer.extend([(float(i), (i,)) for i in range(9)])
        plot.set_series(buffer, ["R0"])
        self.assertEqual(plot.time_range(), (4.0, 8.0))
        self.assertFalse(plot.grab().isNull())
        send_plot_wheel(plot, -120)
        self.assertEqual(plot.time_range(), (3.5, 8.5))
        plot.set_zoom(1.0)
        self.assertEqual(plot.time_range(), (3.0, 8.0))

    def test_fast_envelope_matches_original_and_hover_reuses_geometry(self):
        buffer = MonitorBuffer(1)
        buffer.extend([(i / 1000, (0xFFFFFFFF if i == 4321 else 5,)) for i in range(10000)])
        rect = QRectF(0, 0, 400, 100)
        original = MonitorPlot.trace(buffer.times, buffer.raw[0], 0, 0, 10, rect, lambda value: value)
        optimized = MonitorPlot.trace(buffer.times, buffer.raw[0], 0, 0, 10, rect, lambda value: value,
                                      lambda lo, hi: buffer.extrema(0, lo, hi))
        self.assertEqual(optimized, original)
        plot = MonitorPlot()
        plot.resize(900, 420)
        plot.set_series(buffer, ['R0'])
        plot.grab()
        with patch.object(buffer, 'extrema', side_effect=AssertionError('hover rebuilt geometry')):
            plot._hover = 500
            self.assertFalse(plot.grab().isNull())


    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_envelope_keeps_a_single_sample_spike(self):
        buffer = MonitorBuffer(1)
        buffer.extend([(i / 1000, (100 if i == 5000 else 0,)) for i in range(10000)])
        rect = QRectF(0, 0, 400, 100)
        points = MonitorPlot.trace(buffer.times, buffer.raw[0], 0, 0.0, 10.0, rect, lambda v: 100 - v)
        self.assertLess(len(points), 400 * 5 + 10)   # decimated, not 10 000 points
        self.assertIn(0.0, [point.y() for point in points])   # the spike's top survives

    def test_sparse_trace_is_a_step_line(self):
        points = MonitorPlot.trace([0.0, 1.0], [0, 10], 0, 0.0, 2.0, QRectF(0, 0, 200, 100), lambda v: -v)
        self.assertEqual(points, [QPointF(0, 0), QPointF(100, 0), QPointF(100, -10)])

    def test_paints_empty_flat_busy_and_hovered_states(self):
        plot = MonitorPlot()
        plot.resize(900, 420)
        self.assertFalse(plot.grab().isNull())   # no series yet
        buffer = MonitorBuffer(3)
        plot.set_series(buffer, ["A", "B", "C"])
        plot.grab()
        buffer.extend([(0.0, (1, 1, None))])
        plot.grab()
        buffer.extend([(i * 0.005, (i, 7, None if i % 50 else 0xFFFFFFF0)) for i in range(1, 4000)])
        for factor in (4.0, 0.5, 1.0):
            plot.set_zoom(factor)
            plot.signed = factor == 1.0
            plot.grab()
        self.assertEqual(plot.time_range(), (0.0, buffer.times[-1]))
        plot.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(500, 100), QPointF(500, 100),
                                        Qt.NoButton, Qt.NoButton, Qt.NoModifier))
        self.assertFalse(plot.grab().isNull())
        self.assertEqual(plot.value_text(0, 3), "0x00000003 · 3")

    def test_nice_steps(self):
        for raw, step in ((0.0007, 0.001), (0.3, 0.5), (1.0, 1), (1.4, 2), (7, 10), (31, 50)):
            self.assertAlmostEqual(nice_step(raw), step)


class SetupGuardTests(unittest.TestCase):
    def test_successful_start_keeps_sampling_channel_open(self):
        from devmem_studio.core import SshSession
        channel = Mock()
        session = SshSession()
        session.client = Mock()
        session.client.get_transport.return_value.open_session.return_value = channel
        with session._monitor_channel(keep_open=True) as opened:
            self.assertIs(opened, channel)
        channel.close.assert_not_called()

    def test_setup_cancellation_and_deadline_close_blocked_channel(self):
        from devmem_studio.core import SshSession, CommandError
        import threading
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                stopped, closed = threading.Event(), threading.Event()
                channel = Mock()
                channel.close.side_effect = closed.set
                session = SshSession()
                session.client = Mock()
                session.client.get_transport.return_value.open_session.return_value = channel
                timer = threading.Timer(0.05, stopped.set) if cancel else None
                if timer:
                    timer.start()
                started = time.monotonic()
                with self.assertRaises(CommandError if cancel else TimeoutError):
                    with session._monitor_channel(stopped, timeout=0.15):
                        self.assertTrue(closed.wait(1))
                self.assertLess(time.monotonic() - started, 1)
                if timer:
                    timer.join()


if __name__ == "__main__":
    unittest.main()
