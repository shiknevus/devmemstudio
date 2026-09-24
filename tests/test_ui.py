"""Interaction regressions for failures, cancellation and session provenance."""
import csv
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import paramiko

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from devmem_studio import top_import
from devmem_studio.core import ConfigStore, DemoSession, ReadbackError, HostKeyChangedError, DEFAULT_LOG_PATH
from devmem_studio.theme import STYLE
from devmem_studio.window import MainWindow

IMPORT_TOP_FIXTURE = """// --- flow_comp_1 --A0001_测试轴---
ec_slv_pul_axis
#(
     .REG_SPACE_BIAS     ( 20'd25600)
    ,.REG_SPACE_SIZE     ( `REG_SPACE_SIZE           )
)
ec_slv_pul_axis_1
(
);
// --- flow_comp_2 --A0002_输出组件---
ec_1do
#(
     .REG_SPACE_BIAS     ( 20'd26624)
    ,.REG_SPACE_SIZE     ( `REG_SPACE_SIZE           )
)
ec_1do_2
(
);
// // --- flow_comp_3 --A0003_禁用组件---
// ec_disabled_uut
// #(
//      .REG_SPACE_BIAS     ( 20'd26112)
// )
// ec_disabled_uut_3
// (
// );
"""


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DEVMEMSTUDIO_IGNORE_OVERRIDES"] = "1"   # isolate from real machine imports
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("msyh.ttc", "consola.ttf"):
            if (fonts / filename).exists():
                QFontDatabase.addApplicationFont(str(fonts / filename))

    def settle(self, predicate=lambda: True, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            if predicate():
                return
            time.sleep(0.005)
        self.fail("GUI did not settle")

    def select_and_wait(self, comp):
        """Select a component and wait for its automatic read to finish."""
        self.window.select_component(comp)
        self.settle(lambda: not self.window._busy and self.window.active_component is comp)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = MainWindow(ConfigStore(Path(self.temp.name) / "settings.json"), persist=False)
        self.window.show()
        self.window.session = DemoSession(self.window._log_emit("ssh"))
        self.window.session.connect()
        self.window._set_connection(True)
        self.window.load_top(self.write_fixture_top())
        # The workbench defaults to the basic view; widen to 全部 for row-level assertions.
        self.select_and_wait(self.window.top_info["components"][0])
        self.window.view_buttons["all"].click()
        self.settle(lambda: not self.window._busy)

    def write_fixture_top(self, name="emcc_mix_top.sv"):
        path = Path(self.temp.name) / name
        path.write_text(IMPORT_TOP_FIXTURE, encoding="utf-8")
        return path

    def row_of(self, name):
        return next(index for index, reg in enumerate(self.window.regs) if reg["name"] == name)

    def tearDown(self):
        self.window.close()
        self.settle(lambda: not self.window._busy and not self.window._stream_workers and not self.window.isVisible())
        self.app.processEvents()
        self.temp.cleanup()

    def test_device_error_stops_polling_and_marks_failed_row(self):
        class FailedDevice(DemoSession):
            def read(self, address, quiet=False):
                raise RuntimeError("simulated device error")
        self.window.session.close()
        self.window.session = FailedDevice(self.window._log_emit("ssh"))
        self.window.session.connect()
        self.window.poll_check.setChecked(True)
        self.settle(lambda: self.window.error_count > 0 and not self.window._busy)
        self.assertFalse(self.window.poll_check.isChecked())
        self.assertIn("simulated device error", self.window.regs[0]["_error"])
        self.assertEqual(self.window.table.item(0, 3).text(), "读取错误")

    def test_serial_connect_flow_and_command_routing(self):
        """串口连接按钮、字段禁用逻辑、命令目标下拉路由串口命令。"""
        from devmem_studio.serial_session import SerialSession
        connected_serial = {}
        class FakeSerialSession(SerialSession):
            def connect(self, port, baud, username, password, timeout):
                connected_serial.update(port=port, baud=baud, username=username, password=password)
            def run(self, command, timeout=8, quiet=False):
                self.last_command = command
                return "out:" + command
            def close(self):
                pass
            @property
            def alive(self):
                return True
        with patch("devmem_studio.window.SerialSession", FakeSerialSession):
            # 下拉不可编辑：模拟枚举出 COM9 后选中它。
            self.window.serial_port.addItem("USB Serial Port (COM9)", "COM9")
            self.window.serial_port.setCurrentIndex(self.window.serial_port.findData("COM9"))
            index = self.window.serial_baud.findText("115200")
            self.window.serial_baud.setCurrentIndex(index)
            self.window.serial_user.setText("root")
            self.window.serial_password.setText("secret")
            self.window._toggle_serial()
            self.settle(lambda: self.window.serial_connected and not self.window._busy)
        self.assertTrue(self.window.serial_connected)
        self.assertEqual(connected_serial, {"port": "COM9", "baud": 115200,
                                            "username": "root", "password": "secret"})
        self.assertEqual(self.window.serial_connect_button.text(), "断开连接")
        # 终端来源=serial 时终端提示符可输入、命令走串口会话。
        idx = self.window.console_source_combo.findData("com")
        self.window.console_source_combo.setCurrentIndex(idx)
        self.settle(lambda: self.window.console_source == "com")
        self.assertTrue(self.window.console.is_interactive())
        self.window.send_command("ls /")
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.serial.last_command, "ls /")
        # 断开串口后字段恢复可编辑
        self.window.disconnect_serial()
        self.assertFalse(self.window.serial_connected)
        self.assertTrue(self.window.serial_port.isEnabled())

    def test_serial_connect_shows_shared_progress_bar(self):
        """serial 握手期间显示与 SSH 连接相同的 3px 不定进度条，结束后收回。"""
        from devmem_studio.serial_session import SerialSession
        release = threading.Event()

        class SlowSerial(SerialSession):
            def connect(self, port, baud, username, password, timeout):
                release.wait(5)   # hold the handshake open so the busy state is observable

            def close(self):
                pass

        with patch("devmem_studio.window.SerialSession", SlowSerial):
            self.window.serial_port.addItem("USB Serial Port (COM9)", "COM9")
            self.window.serial_port.setCurrentIndex(self.window.serial_port.findData("COM9"))
            self.window._toggle_serial()
            self.settle(lambda: self.window._serial_busy)
            self.assertTrue(self.window.progress.isVisible())
            self.assertEqual(self.window.progress.maximum(), 0)   # 0..0 = indeterminate
            self.assertIn("连接中", self.window.com_badge.text())
            self.assertEqual(self.window.com_badge.property("state"), "connecting")
            release.set()
            self.settle(lambda: self.window.serial_connected and not self.window._serial_busy)
        self.assertTrue(self.window.progress.isHidden())
        self.assertIn("已连接", self.window.com_badge.text())
        self.assertEqual(self.window.com_badge.property("state"), "connected")

    def test_irq_report_inspector_tooltips_and_export_use_requested_formats(self):
        samples = {
            "IRQ_REG1": (0x48D2ABCD, {"ec_id": "4660", "sc_id": "683", "r_a_bhv_id": "205"}),
            "IRQ_REG2": (0xAABB0000, {"r_a_tx_id": "170", "r_a_alm_num": "187"}),
            **{name: (0xABCDA57F, {"ack_beh_id": "171", "ack_tx_id": "205", "ack_tx_result": "0xA5", "ack_ps_alart_num": "127"})
               for name in ("A_TX_RSULT_RPT", "B_TX_RSULT_RPT", "C_TX_RSULT_RPT")},
        }
        for reg in self.window.regs:
            if reg["name"] not in samples:
                continue
            with self.subTest(name=reg["name"]):
                value, expected = samples[reg["name"]]
                self.window.session.memory[reg["_address"]] = value
                self.window.table.selectRow(reg["_row"])
                self.window.bit_mode.click()
                self.window.read_selected()
                self.settle(lambda: not self.window._busy)
                self.assertTrue(self.window.decoded_view.isVisible())
                self.assertEqual({key: cell.text() for key, cell in self.window.decoded_view.field_values.items()}, expected)
                tip = self.window.table.item(reg["_row"], 3).toolTip()
                row = next(row for row in csv.DictReader(io.StringIO(self.window.snapshot_csv())) if row["寄存器"] == reg["name"])
                for key, number in expected.items():
                    self.assertIn(f"{key}={number}", tip)
                    self.assertIn(f"{key}={number}", row["字段解析"])

    def test_failed_read_clears_decoded_values_in_view_and_export(self):
        self.window.table.selectRow(self.row_of("A_TX_RSULT_RPT"))
        self.window.read_selected()
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.decoded_view.field_values["ack_tx_result"].text(), "0x01")
        with patch.object(self.window.session, "read", side_effect=RuntimeError("simulated read failure")):
            self.window.read_selected()
            self.settle(lambda: not self.window._busy)
        self.assertTrue(all(cell.text() == "—" for cell in self.window.decoded_view.field_values.values()))
        self.assertIn("simulated read failure", self.window.decoded_label.text())
        row = next(row for row in csv.DictReader(io.StringIO(self.window.snapshot_csv())) if row["寄存器"] == "A_TX_RSULT_RPT")
        self.assertEqual(row["字段解析"], "")

    def test_component_write_address_and_draft_memory(self):
        self.window.base_field.setText("0xB0200000")
        self.window._base_edited()
        self.settle()
        index = self.row_of("EC_ID")
        self.assertEqual(self.window.regs[0]["_address"], 0xB0200000 + 0x6400 + 0x000)
        self.assertEqual(self.window.regs[index]["_address"], 0xB020640C)
        self.window.table.selectRow(index)
        self.window.format_combo.setCurrentIndex(0)   # HEX, otherwise "ABCD" fails to parse
        self.window.write_input.setText("ABCD")
        self.assertIn("devmem 0xb020640c 32 0xabcd", self.window.command_preview.text())
        self.window.write_button.click()
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.selected["_value"], 0xABCD)
        self.assertEqual(self.window.session.memory[0xB020640C], 0xABCD)
        self.select_and_wait(self.window.top_info["components"][1])
        self.assertNotEqual(self.window.cache_key, "top:ec_slv_pul_axis@0x6400")
        self.select_and_wait(self.window.top_info["components"][0])
        self.window.table.selectRow(self.row_of("EC_ID"))
        self.assertEqual(self.window.write_input.text(), "ABCD")
        self.assertIn("A0001", self.window.module_title.text())

    def test_failed_readback_counts_completed_write_and_stops_batch(self):
        class FailedReadback(DemoSession):
            def write(self, address, width, value, quiet=False):
                self.memory[address] = value
                raise ReadbackError("写入已完成，但回读失败：模拟超时")
        self.window.session.close()
        self.window.session = FailedReadback(self.window._log_emit("ssh"))
        self.window.session.connect()
        plans = [reg for reg in self.window.visible_regs if not reg.get("readonly")][:2]
        for reg in plans:
            self.window._prepare_write(reg)
        self.window.table.selectRow(plans[0]["_row"])
        reads_before = self.window.read_count
        self.window._write_many(plans)
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.write_count, 1)
        self.assertEqual(self.window.read_count, reads_before)
        self.assertEqual(len(self.window.session.memory), 1)
        self.assertEqual(self.window.table.item(plans[0]["_row"], 3).text(), "回读失败")
        self.assertIn("写入已完成", self.window.dec_value.text())

    def test_close_during_pending_read_releases_worker(self):
        self.window.read_all()
        self.assertTrue(self.window._busy)
        self.window.close()
        self.settle(lambda: not self.window.isVisible())
        self.assertFalse(self.window._busy)
        self.assertFalse(self.window.session.alive)

    def test_invalid_base_never_uses_previous_context(self):
        self.window.base_field.setText("not-an-address")
        self.window._base_edited()
        self.settle()
        self.assertFalse(self.window.read_all_button.isEnabled())
        previous = self.window.read_count
        self.window.read_all()
        self.assertEqual(previous, self.window.read_count)
        self.window.base_field.setText("0xb0100000")
        self.window._base_edited()
        self.settle()
        self.assertTrue(self.window.read_all_button.isEnabled())

    def test_demo_provenance_survives_disconnect_and_host_edit(self):
        self.window.read_all()
        self.settle(lambda: not self.window._busy)
        self.window.disconnect()
        self.window.host.setText("new-target")
        rows = list(csv.reader(io.StringIO(self.window.snapshot_csv())))
        self.assertEqual(rows[1][0], "离线演示")
        self.assertEqual(rows[1][1], "DEMO")
        self.assertEqual(rows[1][-1], "离线缓存")

    def test_preset_stays_synchronized_with_numeric_editor(self):
        self.window.table.selectRow(self.row_of("A_BHV_ID"))
        self.window.preset_combo.setCurrentIndex(2)
        self.assertEqual(self.window.write_input.text(), "2")
        self.window.format_combo.setCurrentIndex(0)   # HEX, otherwise "15" parses as decimal
        self.window.write_input.setText("15")
        self.assertEqual(self.window.preset_combo.currentData(), 21)

    def test_previous_stream_callback_does_not_stop_new_stream(self):
        # Connect landed the console on the ssh view and started the demo stream in the background.
        self.settle(lambda: self.window._stream_state == "running")
        previous = self.window._stream_epoch
        self.window._stop_stream()
        self.window._start_stream("/tmp/second.log")
        self.settle(lambda: self.window._stream_state == "running")
        # Stale callbacks from the previous epoch must not flip the new stream's state.
        self.window.bridge.stream_stopped.emit(previous)
        self.window.bridge.stream_ready.emit(previous, False)
        self.window.bridge.stream_failed.emit(previous, "cancelled old request")
        self.app.processEvents()
        self.assertEqual(self.window._stream_state, "running")
        self.window._stop_stream()

    def test_one_click_log_start_does_not_block_read_write_and_can_be_cancelled(self):
        entered = threading.Event()
        paths = []
        class SlowLogDevice(DemoSession):
            def start_stream(self, path, output, stopped, cancel_event=None):
                paths.append(path)
                entered.set()
                cancel_event.wait(3)
                return super().start_stream(path, output, stopped, cancel_event=cancel_event)
        # Stop the auto-started demo stream, swap in the slow device, then restart inline.
        self.window._stop_stream()
        self.window.session.close()
        self.window.session = SlowLogDevice(self.window._log_emit("ssh"))
        self.window.session.connect()
        # No auto-jump on connect: the console stays on system until switched manually.
        self.assertEqual(self.window.console_source, "system")
        self.assertFalse(self.window.console.is_interactive())
        self.window.read_all()
        self.assertTrue(self.window._busy)
        # Log start does not block register reads: the read task is queued alongside.
        self.window._ensure_log_stream()
        self.assertEqual(self.window._task_kind, "read")
        self.settle(entered.is_set)
        self.assertEqual(self.window._stream_state, "starting")
        self.assertEqual(paths, [DEFAULT_LOG_PATH])
        self.assertEqual(len(self.window._stream_workers), 1)
        self.settle(lambda: not self.window._busy)
        self.assertTrue(self.window.read_all_button.isEnabled())
        self.window.table.selectRow(self.row_of("A_TX_OT"))
        self.window.format_combo.setCurrentIndex(0)   # HEX, otherwise "37" parses as decimal
        self.window.write_input.setText("37")
        self.window.write_button.click()
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.selected["_value"], 0x37)
        self.window._stop_stream()
        self.settle(lambda: not self.window._stream_workers)
        self.assertEqual(self.window._stream_state, "stopped")
        self.assertTrue(self.window.session.alive)
        self.assertTrue(self.window.read_all_button.isEnabled())

    def test_log_start_failure_does_not_stop_register_polling(self):
        class FailedLogDevice(DemoSession):
            def start_stream(self, path, output, stopped, cancel_event=None):
                raise OSError("simulated log channel failure")
        # Stop the auto-started demo stream, swap in the failing device, restart inline.
        self.window._stop_stream()
        self.window.session.close()
        self.window.session = FailedLogDevice(self.window._log_emit("ssh"))
        self.window.session.connect()
        self.window.poll_check.setChecked(True)
        self.window._ensure_log_stream()
        self.settle(lambda: not self.window._stream_workers)
        # The failure message is software bookkeeping: it lands in the system log.
        self.assertTrue(any("simulated log channel failure" in m for _, _, m in self.window.log_records_system))
        self.assertTrue(self.window.poll_check.isChecked())
        self.assertTrue(self.window.connected and self.window.session.alive)
        self.window.poll_check.setChecked(False)
        self.settle(lambda: not self.window._busy)
        self.assertTrue(self.window.read_all_button.isEnabled())

    def test_log_find_shortcut_and_live_search_leave_main_register_write_available(self):
        # The console starts on system; switch to the log source to see the stream.
        self.settle(lambda: self.window._stream_state == "running")
        self.window._set_console_source("log")
        self.settle(lambda: "axis" in self.window.console.toPlainText())
        self.assertEqual(self.window.console_source, "log")
        self.window.log_search.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.console, Qt.Key_F, Qt.ControlModifier)
        self.assertTrue(self.window.log_search.hasFocus())
        self.assertFalse(self.window.search.hasFocus())
        self.window.log_search.setText("axis")
        self.window._find_in_log(forward=True)
        cursor = self.window.console.textCursor()
        self.assertTrue(cursor.hasSelection())
        self.assertIn("axis", cursor.selectedText())
        position = cursor.selectionStart()
        self.window.table.selectRow(self.row_of("A_TX_OT"))
        self.window.format_combo.setCurrentIndex(0)   # HEX, otherwise "39" parses as decimal
        self.window.write_input.setText("39")
        self.window.write_button.click()
        self.window._append_log_stream("INFO [DEMO] axis new data\n" * 300)
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.selected["_value"], 0x39)
        # The match position survives appended log text (cursor stays at the match).
        self.assertEqual(self.window.console.textCursor().selectionStart(), position)
        self.assertTrue(self.window.session.alive)

    def exercise_host_key_dialog(self, decision):
        self.window.disconnect()
        self.window.host.setText("192.0.2.10")
        self.window.port.setValue(2222)
        self.window.user.setText("test")
        self.window.password.setText("test")
        change = HostKeyChangedError("192.0.2.10", 2222, paramiko.RSAKey.generate(2048),
                                     paramiko.RSAKey.generate(2048))
        requests, approvals = [], []
        class ChangedDevice(DemoSession):
            def connect(self, *args):
                requests.append(args)
                if not approvals:
                    raise change
                super().connect()

            def replace_host_key(self, observed):
                approvals.append(observed)

        with patch("devmem_studio.window.SshSession", ChangedDevice), \
                patch("devmem_studio.window.HostKeyDialog") as dialog:
            dialog.return_value.exec.return_value = decision
            self.window.connect_button.click()
            self.settle(lambda: dialog.call_count > 0 and not self.window._busy)
            if decision == QDialog.Accepted:
                self.settle(lambda: self.window.connected and not self.window._busy)
            dialog.assert_called_once_with(change, self.window)
        self.assertEqual(self.window.connected, decision == QDialog.Accepted)
        self.assertEqual(approvals, [change] if decision == QDialog.Accepted else [])
        self.assertEqual(len(requests), 2 if decision == QDialog.Accepted else 1)
        self.assertTrue(all(request == requests[0] for request in requests))
        self.assertEqual(requests[0][:4], ("192.0.2.10", 2222, "test", "test"))
        self.assertTrue(self.window.connect_button.isEnabled())
        self.assertIsNone(self.window._pending_host_key_change)

    def test_host_key_confirmation_retries_with_exact_approved_key(self):
        self.exercise_host_key_dialog(QDialog.Accepted)
        self.assertTrue(self.window.read_all_button.isEnabled())

    def test_host_key_cancellation_preserves_trust_and_stays_disconnected(self):
        self.exercise_host_key_dialog(QDialog.Rejected)
        self.assertTrue(any("已取消主机密钥更新" in text for _, _, text in self.window.log_records_system))
        self.assertFalse(self.window.read_all_button.isEnabled())

    def test_pul_axis_abspos_actual_value_row(self):
        # 实际值 = 补码(r_pf_abspos) / PARAM4，浮点；PARAM4 为 0/未读显示 —.
        self.window.view_buttons["param"].click()
        self.settle(lambda: not self.window._busy)
        abspos = self.window.regs[self.row_of("PARAM51")]
        param4 = self.window.regs[self.row_of("PARAM4")]
        self.assertEqual(self.window._pulse_scaled_label(abspos), "实际值 (mm | °)")
        self.assertIsNone(self.window._pulse_scaled_label(param4))
        # PARAM4 未读取 → 派生行显示 —.
        self.window.session.memory[abspos["_address"]] = 0xFFFFFF9C
        self.window.table.selectRow(abspos["_row"])
        self.window.bit_mode.click()
        self.window.read_selected()
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.decoded_view.field_values["实际值 (mm | °)"].text(), "—")
        # PARAM4=4, abspos=-100（0xFFFFFF9C 补码）→ -25.
        self.window.session.memory[param4["_address"]] = 4
        self.window.read_register(param4)
        self.settle(lambda: not self.window._busy)
        self.window.read_register(abspos)
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.decoded_view.field_values["实际值 (mm | °)"].text(), "-25")
        # 正数不受补码分支影响：abspos=1000, param4=4 → 250.
        self.window.session.memory[abspos["_address"]] = 1000
        self.window.read_register(abspos)
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.decoded_view.field_values["实际值 (mm | °)"].text(), "250")
        # PARAM4=0 → 显示 —.
        self.window.session.memory[param4["_address"]] = 0
        self.window.read_register(param4)
        self.settle(lambda: not self.window._busy)
        self.window.read_register(abspos)
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.decoded_view.field_values["实际值 (mm | °)"].text(), "—")

    def test_pul_axis_speed_accel_scaled_rows(self):
        # 速度/加减速族同样按 /PARAM4 换算：spd→mm/s|°/s，acc/dec→mm/s²|°/s².
        self.window.view_buttons["param"].click()
        self.settle(lambda: not self.window._busy)
        param4 = self.window.regs[self.row_of("PARAM4")]
        self.window.session.memory[param4["_address"]] = 1000
        self.window.read_register(param4)
        self.settle(lambda: not self.window._busy)
        self.window.bit_mode.click()
        for name, label, expected in (("PARAM35", "实际值 (mm/s | °/s)", "12.5"),
                                      ("PARAM33", "实际值 (mm/s | °/s)", "0.8"),
                                      ("PARAM5", "实际值 (mm/s² | °/s²)", "25"),
                                      ("PARAM2", "实际值 (mm/s² | °/s²)", "250"),
                                      ("PARAM3", "实际值 (mm/s² | °/s²)", "2.5")):
            with self.subTest(name=name):
                reg = self.window.regs[self.row_of(name)]
                self.assertEqual(self.window._pulse_scaled_label(reg), label)
                self.window.session.memory[reg["_address"]] = int(float(expected) * 1000)
                self.window.read_register(reg)
                self.settle(lambda: not self.window._busy)
                self.window.table.selectRow(reg["_row"])
                self.assertEqual(self.window.decoded_view.field_values[label].text(), expected)

    def test_pul_axis_soft_limit_positions_parse_like_position(self):
        # 最大位置/最小位置（rcfg_pos_max/min，软限位）与目标脉冲/步进脉冲
        # （rserv_target/step_pulse）都走 /PARAM4 的实际值换算，解析栏不再显示
        # 位段：PARAM31/32/36/37 与 r_pf_abspos 同族。
        self.window.view_buttons["param"].click()
        self.settle(lambda: not self.window._busy)
        param4 = self.window.regs[self.row_of("PARAM4")]
        self.window.session.memory[param4["_address"]] = 4
        self.window.read_register(param4)
        self.settle(lambda: not self.window._busy)
        self.window.bit_mode.click()
        for name, raw, expected in (("PARAM31", 0xFFFFFF9C, "-25"),
                                    ("PARAM32", 500, "125"),
                                    ("PARAM36", 0xFFFFFF9C, "-25"),
                                    ("PARAM37", 500, "125")):
            with self.subTest(name=name):
                reg = self.window.regs[self.row_of(name)]
                self.assertEqual(self.window._pulse_scaled_label(reg), "实际值 (mm | °)")
                # 有符号：-100 → -100/4=-25；正数：500 → 500/4=125.
                self.window.session.memory[reg["_address"]] = raw
                self.window.read_register(reg)
                self.settle(lambda: not self.window._busy)
                self.window.table.selectRow(reg["_row"])
                values = self.window.decoded_view.field_values
                self.assertEqual(list(values), ["实际值 (mm | °)"])   # 无位段
                self.assertEqual(values["实际值 (mm | °)"].text(), expected)
                # CSV 导出的“字段解析”同样只要实际值，不带位段。
                row = next(item for item in csv.reader(io.StringIO(self.window.snapshot_csv()))
                           if item[3].startswith(name))
                self.assertEqual(row[12], f"实际值 (mm | °)={expected}")

    def test_top_import_drops_stale_row_button_references(self):
        # 回归：导入新 top 后 rebuild_registers 的空组件分支必须清掉旧行按钮引用，
        # 否则 _refresh_controls 会对已销毁的 QPushButton 调 setEnabled 崩溃
        # （libshiboken: Internal C++ object ... already deleted）。
        self.window.view_buttons["all"].click()
        self.settle(lambda: not self.window._busy)
        self.assertTrue(self.window.row_buttons)
        self.window.load_top(self.write_fixture_top("second-top.sv"))
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.row_buttons, [])
        # 空组件分支后刷新控制不应崩溃。
        self.window._refresh_controls()

    def test_refresh_controls_skips_already_deleted_row_buttons(self):
        # 回归：即使某个行按钮的 C++ 对象已被 Qt 销毁（setEnabled 抛
        # RuntimeError），_refresh_controls 也必须跳过而不是继续崩溃，并把
        # 失效按钮移出 row_buttons。
        self.window.view_buttons["all"].click()
        self.settle(lambda: not self.window._busy)
        count = len(self.window.row_buttons)
        stale = StaleDeletedButton()
        self.window.row_buttons.append(stale)
        self.window._refresh_controls()   # 不应抛异常
        self.assertNotIn(stale, self.window.row_buttons)
        self.assertEqual(len(self.window.row_buttons), count)   # 其余按钮保留

    def test_selecting_any_register_defaults_to_bit_mode(self):
        # 所有寄存器（含可写寄存器）选中后默认落在“解析与位状态”页签。
        self.window.view_buttons["all"].click()
        self.settle(lambda: not self.window._busy)
        writable = [reg for reg in self.window.regs if not reg.get("readonly")]
        readonly = [reg for reg in self.window.regs if reg.get("readonly")]
        self.assertTrue(writable and readonly)
        for selector, pool in (("可写", writable), ("只读", readonly)):
            reg = pool[0]
            self.window.table.selectRow(reg["_row"])
            self.settle(lambda: not self.window._busy)
            self.assertTrue(self.window.bit_mode.isChecked(), f"{selector}寄存器应默认选中解析与位状态")
            self.assertFalse(self.window.write_mode.isChecked())


class StaleDeletedButton:
    """Mimics a PySide6 wrapper whose C++ QPushButton is already destroyed."""
    def setEnabled(self, enabled):
        raise RuntimeError("Internal C++ object (PySide6.QtWidgets.QPushButton) already deleted.")


class TopImportUiTests(UiTests):
    def test_import_populates_tree_and_selects_exact_registers(self):
        self.window.load_top(self.write_fixture_top())
        self.assertEqual(self.window.component_tree.topLevelItemCount(), 3)
        self.assertIn("ec_slv_pul_axis (1)", self.window.component_tree.topLevelItem(0).text(0))
        disabled_item = self.window.component_tree.topLevelItem(2).child(0)
        self.assertFalse(bool(disabled_item.flags() & Qt.ItemIsEnabled))
        target = next(item for item in self.window.top_info["components"] if not item["disabled"]
                      and item["module_type"] == "ec_slv_pul_axis")
        self.window.select_component(target)
        self.assertTrue(self.window.component_mode)
        self.assertEqual(self.window.cache_key, "top:ec_slv_pul_axis@0x6400")
        self.assertEqual(self.window.module_badge.text(), "ec_slv_pul_axis")
        names = [reg["name"] for reg in self.window.regs]
        self.assertEqual(names[0], "IRQ_REG1")
        self.assertIn("EC_ID", names)
        self.window.read_all_button.click()
        self.settle(lambda: not self.window._busy)
        self.assertTrue(all(reg["_value"] is not None for reg in self.window.visible_regs))
        self.window.select_component(next(item for item in self.window.top_info["components"]
                                          if item["disabled"]))
        self.assertEqual(self.window.cache_key, "top:ec_slv_pul_axis@0x6400")  # disabled is ignored

    def test_group_header_toggles_on_single_click(self):
        group = self.window.component_tree.topLevelItem(1)
        self.assertFalse(group.isExpanded())
        self.window.component_tree.itemClicked.emit(group, 0)
        self.assertTrue(group.isExpanded())
        self.window.component_tree.itemClicked.emit(group, 0)
        self.assertFalse(group.isExpanded())

    def test_component_search_filters_tree(self):
        self.window.component_search.setText("A0001")
        self.settle()
        visible = [self.window.component_tree.topLevelItem(index).child(child)
                   for index in range(self.window.component_tree.topLevelItemCount())
                   if not self.window.component_tree.topLevelItem(index).isHidden()
                   for child in range(self.window.component_tree.topLevelItem(index).childCount())
                   if not self.window.component_tree.topLevelItem(index).child(child).isHidden()]
        self.assertEqual([item.text(0).split(" ·")[0].strip() for item in visible], ["A0001_测试轴"])
        self.window.component_search.setText("不存在的东西")
        self.settle()
        self.assertTrue(all(self.window.component_tree.topLevelItem(index).isHidden()
                            for index in range(self.window.component_tree.topLevelItemCount())))

    def test_view_tabs_slice_exact_table(self):
        expectations = {
            "all": lambda names: len(names) == len(self.window.regs),
            "basic": lambda names: "RST_EN" in names and "IRQ_REG1" not in names and "A_EN" not in names,
            "task_a": lambda names: {"A_EN", "A_BHV_ID", "A_TASK_ID", "A_TX_OT", "EC_CHA_ST"} <= set(names)
                                     and "B_EN" not in names and "C_EN" not in names and "EC_ID" not in names,
            "task_b": lambda names: {"B_EN", "B_BHV_ID", "B_TX_OT", "EC_CHB_ST"} <= set(names)
                                     and "A_EN" not in names and "C_EN" not in names,
            "task_c": lambda names: {"C_EN", "C_BHV_ID", "C_TX_OT", "C_GAP_CRL", "EC_CHC_ST"} <= set(names)
                                     and "A_EN" not in names and "B_EN" not in names,
            "irq": lambda names: names == ["IRQ_REG1", "IRQ_REG2", "A_TX_RSULT_RPT",
                                           "B_TX_RSULT_RPT", "C_TX_RSULT_RPT"],
            "param": lambda names: names and all(name.startswith("PARAM") for name in names),
            "debug": lambda names: names == ["DEBUG_REG1", "DEBUG_REG2", "DEBUG_REG3"],
        }
        for view in top_import.VIEW_ORDER:
            with self.subTest(view=view):
                self.window.view_buttons[view].click()
                self.settle(lambda: not self.window._busy)
                names = [reg["name"] for reg in self.window.visible_regs]
                self.assertTrue(expectations[view](names), names)
                self.assertEqual(names, [self.window.regs[index]["name"]
                                         for index in range(len(self.window.regs))
                                         if not self.window.table.isRowHidden(index)])
        before = self.window.read_count
        self.window.view_buttons["task_a"].click()
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.read_count, before + len(self.window.visible_regs))
        before = self.window.read_count
        self.window.read_all_button.click()
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.read_count, before + len(self.window.visible_regs))

    def test_signed_register_displays_twos_complement(self):
        # r_pf_abspos is declared signed in the RTL: 0xFFFFFF9C must display as -100.
        self.window.view_buttons["param"].click()
        self.settle(lambda: not self.window._busy)
        row = self.row_of("PARAM51")
        self.assertTrue(self.window.regs[row].get("signed"))
        self.window.session.memory[self.window.regs[row]["_address"]] = 0xFFFFFF9C
        self.window.table.selectRow(row)
        self.window.read_selected()
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.table.item(row, 4).text(), "-100")
        self.assertIn("DEC  -100", self.window.dec_value.text())
        csv_row = next(item for item in csv.reader(io.StringIO(self.window.snapshot_csv()))
                      if item[3].startswith("PARAM51"))
        self.assertEqual(csv_row[11], "-100")
        # A positive value stays untouched.
        self.window.session.memory[self.window.regs[row]["_address"]] = 1000
        self.window.read_selected()
        self.settle(lambda: not self.window._busy)
        self.assertEqual(self.window.table.item(row, 4).text(), "1000")
        # Unsigned registers are unaffected.
        self.window.table.selectRow(self.row_of("PARAM1"))
        self.window.read_selected()
        self.settle(lambda: not self.window._busy)
        pos = self.window.regs[self.row_of("PARAM1")]
        self.assertEqual(self.window.table.item(pos["_row"], 4).text(), str(pos["_value"]))

    def test_param_rows_show_actual_signal_names(self):
        self.window.view_buttons["param"].click()
        self.settle(lambda: not self.window._busy)
        row = self.row_of("PARAM1")
        self.assertIn("rcfg_spd_max", self.window.table.item(row, 0).text())
        self.assertIn("rcfg_spd_max", self.window.table.item(row, 0).toolTip())
        concat = self.row_of("PARAM64")
        self.assertIn("i_axis_limf", self.window.table.item(concat, 0).text())
        unwired = self.row_of("PARAM4")
        self.assertIn("未接线", self.window.table.item(unwired, 0).text())
        self.assertIn("读回值无意义", self.window.table.item(unwired, 0).toolTip())
        # PARAM52 concat: every wire becomes its own field row, values live.
        row52 = self.row_of("PARAM52")
        self.window.session.memory[self.window.regs[row52]["_address"]] = 0b110
        self.window.table.selectRow(row52)
        self.window.read_selected()
        self.settle(lambda: not self.window._busy)
        self.assertEqual({k: c.text() for k, c in self.window.decoded_view.field_values.items()},
                         {"b_reset": "1", "param16[0]": "0"})
        self.assertEqual([c.text() for c in self.window.decoded_view.field_values.values()],
                         ["1", "0"])   # one RTL wire per line
        self.window.search.setText("r_pf_abspos")
        self.settle()
        self.assertEqual([reg["name"] for reg in self.window.visible_regs], ["PARAM51"])
        rows = list(csv.reader(io.StringIO(self.window.snapshot_csv())))
        param_row = next(item for item in rows if item[3].startswith("PARAM1 "))
        self.assertIn("rcfg_spd_max", param_row[3])

    def test_view_empty_state_for_fallback_type(self):
        self.window.type_catalog = {"types": {}}
        self.window.load_top(self.write_fixture_top())
        self.select_and_wait(self.window.top_info["components"][0])
        self.assertTrue(self.window.module_badge.text().endswith("≈"))
        self.assertEqual(len(self.window.regs), len(top_import.FALLBACK_REGISTERS))
        self.window.view_buttons["debug"].click()
        self.settle()
        self.assertTrue(self.window.empty_label.isVisibleTo(self.window.table.parentWidget()))
        self.assertIn("没有寄存器", self.window.empty_label.text())
        self.window.read_all()
        self.assertTrue(any("没有可读取" in text for _, _, text in self.window.log_records_system))
        self.assertTrue(any("通用回退" in message for _, _, message in self.window.log_records_system))

    def test_upload_download_available_without_selected_component(self):
        # Connected but no component chosen: upload/download must stay usable.
        self.window.select_component(self.window.top_info["components"][0])
        self.settle(lambda: not self.window._busy)
        self.assertTrue(self.window.bit_upload_button.isEnabled())
        self.assertTrue(self.window.bit_rollback_button.isEnabled())
        self.assertTrue(self.window.log_download_button.isEnabled())
        self.window.active_component = None
        self.window.rebuild_registers()
        self.settle()
        self.assertFalse(self.window.read_all_button.isEnabled())   # register ops stay locked
        self.assertTrue(self.window.bit_upload_button.isEnabled())   # file ops stay unlocked
        self.assertTrue(self.window.bit_rollback_button.isEnabled())
        self.assertTrue(self.window.log_download_button.isEnabled())

    def test_bit_rollback_lists_backups_and_restores_selected(self):
        # Seed the demo board with a current bit and two timestamped backups.
        sda = self.window.session.remote_root / "run" / "media" / "sda"
        sda.mkdir(parents=True, exist_ok=True)
        (sda / "sunny_fpga.bit").write_bytes(b"\x00" * 100)
        (sda / "sunny_fpga.bit_20260916000000").write_bytes(b"\x01" * 100)
        (sda / "sunny_fpga.bit_20260918000000").write_bytes(b"\x02" * 100)
        self.window.bit_rollback_button.click()
        self.settle(lambda: self.window.bit_rollback_dialog and
                    not self.window._busy and self.window.bit_rollback_dialog.list.count() == 3)
        dialog = self.window.bit_rollback_dialog
        rows = [dialog.list.item(i).text() for i in range(dialog.list.count())]
        self.assertTrue(any("当前版本" in text for text in rows))
        self.assertTrue(any("备份于" in text for text in rows))
        # The live bit must not be selectable as a rollback target; newest backup preselected.
        current = next(i for i in range(dialog.list.count())
                       if "当前版本" in dialog.list.item(i).text())
        self.assertFalse(dialog.list.item(current).flags() & Qt.ItemIsEnabled)
        self.assertEqual(dialog.selected_backup(), "sunny_fpga.bit_20260918000000")
        # Double-click the chosen backup, confirm, and verify the rename sequence.
        with patch("devmem_studio.window.QMessageBox.question", return_value=QMessageBox.Yes) as question:
            dialog.list.item(1).setSelected(True)
            dialog.list.setCurrentRow(1)
            dialog.list.itemDoubleClicked.emit(dialog.list.item(1))
            self.settle(lambda: not self.window._busy)
            question.assert_called_once()
        sda = self.window.session.remote_root / "run" / "media" / "sda"
        self.assertTrue((sda / "sunny_fpga.bit").is_file())           # restored
        self.assertEqual((sda / "sunny_fpga.bit").read_bytes(), b"\x02" * 100)
        self.assertTrue(any(p.name.startswith("sunny_fpga.bit_") for p in sda.iterdir()
                            if p.name not in ("sunny_fpga.bit_20260916000000",
                                              "sunny_fpga.bit_20260918000000")))   # new aside backup
        self.assertIn("回退完成", " ".join(message for _, _, message in self.window.log_records_system))

    def test_bit_rollback_cancel_leaves_files_untouched(self):
        sda = self.window.session.remote_root / "run" / "media" / "sda"
        sda.mkdir(parents=True, exist_ok=True)
        (sda / "sunny_fpga.bit").write_bytes(b"\x00" * 100)
        (sda / "sunny_fpga.bit_20260916000000").write_bytes(b"\x01" * 100)
        self.window.bit_rollback_button.click()
        self.settle(lambda: self.window.bit_rollback_dialog and
                    not self.window._busy and self.window.bit_rollback_dialog.list.count() == 2)
        dialog = self.window.bit_rollback_dialog
        with patch("devmem_studio.window.QMessageBox.question", return_value=QMessageBox.No):
            dialog.list.setCurrentRow(1)
            dialog.rollback_button.click()
            self.settle(lambda: not self.window._busy)
        self.assertEqual((sda / "sunny_fpga.bit").read_bytes(), b"\x00" * 100)
        self.assertEqual((sda / "sunny_fpga.bit_20260916000000").read_bytes(), b"\x01" * 100)
        # No new aside backup created on cancel: only current + the one seeded backup remain.
        bit_files = [p.name for p in sda.iterdir() if p.name.startswith("sunny_fpga.bit")]
        self.assertEqual(sorted(bit_files), ["sunny_fpga.bit", "sunny_fpga.bit_20260916000000"])

    def test_empty_state_without_component(self):
        window = MainWindow(ConfigStore(Path(self.temp.name) / "empty.json"), persist=False)
        try:
            window.show()
            self.settle()
            self.assertFalse(window.component_mode)
            self.assertEqual(window.regs, [])
            self.assertTrue(window.empty_label.isVisibleTo(window.table.parentWidget()))
            window.read_all()
            self.assertTrue(any("选择组件" in text for _, _, text in window.log_records_system))
            self.assertFalse(window.read_all_button.isEnabled() or window.poll_check.isEnabled())
            window.session = DemoSession(window._log_emit("ssh"))
            window.session.connect()
            window._set_connection(True)
            window.read_all()
            self.assertTrue(any("选择组件" in text for _, _, text in window.log_records_system))
        finally:
            window.close()
            self.settle(lambda: not window._busy and not window.isVisible())

    def test_base_fallback_default_and_rtl_override(self):
        self.assertTrue(self.window.base_field.isEnabled())
        self.assertEqual(self.window.base_field.text().lower(), "0xb0100000")
        self.assertIsNone(self.window.rtl_base)
        root = Path(self.temp.name)
        (root / "include_files").mkdir()
        (root / "include_files" / "components_param.vh").write_text(
            "`define PL_CFG_BASE_ADDR {32'hB020_0000}\n", encoding="utf-8")
        self.write_fixture_top("rtl-top.sv")
        self.window.load_top(root / "rtl-top.sv")
        self.settle()
        self.assertEqual(self.window.rtl_base, 0xB0200000)
        self.assertFalse(self.window.base_field.isEnabled())
        self.window.select_component(self.window.top_info["components"][0])
        self.settle()
        self.assertEqual(self.window.regs[0]["_address"], 0xB0200000 + 0x6400 + 0x000)

    def test_behavior_presets_notes_and_debug_decode(self):
        self.window.view_buttons["task_a"].click()
        self.settle(lambda: not self.window._busy)
        self.window.table.selectRow(self.row_of("A_BHV_ID"))
        # 选中默认落在“解析与位状态”，写设置（含预设）需切到“写入设置”页。
        self.window.write_mode.click()
        self.assertEqual(self.window.preset_combo.itemText(1).strip(), "home   (1)")
        self.assertTrue(self.window.preset_combo.isVisible())
        self.assertIn("fwd limit", self.window.table.item(self.row_of("PARAM64"), 0).toolTip())
        self.window.view_buttons["debug"].click()
        self.settle(lambda: not self.window._busy)
        row = self.row_of("DEBUG_REG1")
        self.window.session.memory[self.window.regs[row]["_address"]] = 0x05030205
        self.window.table.selectRow(row)
        self.window.read_selected()
        self.settle(lambda: not self.window._busy)
        self.assertEqual({key: cell.text() for key, cell in self.window.decoded_view.field_values.items()},
                         {"前第3拍": "5", "前第2拍": "3", "前第1拍": "2", "当前": "5"})
        self.assertIn("状态机历史", self.window.regs[row]["_note"])

    def test_bhv_en_not_offered(self):
        self.window.type_catalog = {"types": {}}
        self.window.load_top(self.write_fixture_top())
        self.window.select_component(self.window.top_info["components"][0])
        self.settle()
        for view in top_import.VIEW_ORDER:
            self.window.view_buttons[view].click()
            self.settle()
            self.assertNotIn("BHV_EN", [reg["name"] for reg in self.window.regs])

    def test_import_requires_disconnect_and_confirmation(self):
        from PySide6.QtWidgets import QMessageBox
        self.assertFalse(self.window.import_button.isEnabled())          # connected: disabled
        self.window.disconnect()
        self.settle(lambda: not self.window._busy)
        self.assertTrue(self.window.import_button.isEnabled())
        fresh = self.write_fixture_top("confirm-top.sv")
        with patch("devmem_studio.window.QFileDialog.getOpenFileName", return_value=(str(fresh), "")), \
                patch("devmem_studio.window.QMessageBox.question", return_value=QMessageBox.No) as question:
            self.window.import_top()
            question.assert_called_once()
            self.assertIn("主板卡死", question.call_args[0][2])          # explicit danger warning
            self.assertNotEqual(self.window.top_info["path"], str(fresh))
        with patch("devmem_studio.window.QFileDialog.getOpenFileName", return_value=(str(fresh), "")), \
                patch("devmem_studio.window.QMessageBox.question", return_value=QMessageBox.Yes):
            self.window.import_top()
            self.assertEqual(self.window.top_info["path"], str(fresh))
            self.assertIn(str(fresh), self.window.top_summary.text())     # path shown for confirmation

    def test_toolbar_stable_during_task_and_cancel_still_works(self):
        # 停止按钮已移除；工具栏在任务进行/结束时不再因按钮显隐而重排，
        # 取消能力由 Esc / cancel_task 保留。
        idle = self.window.write_all_button.geometry()
        self.window.read_all()
        self.assertTrue(self.window._busy)
        busy = self.window.write_all_button.geometry()
        self.assertEqual((idle.x(), idle.y()), (busy.x(), busy.y()))
        self.settle(lambda: not self.window._busy)
        after = self.window.write_all_button.geometry()
        self.assertEqual((idle.x(), idle.y()), (after.x(), after.y()))
        # 任务运行期间取消请求仍被接受（Esc 快捷键仍绑定 cancel_task）。
        self.window.read_all()
        self.assertTrue(self.window._busy)
        self.window.cancel_task()
        self.assertTrue(self.window.cancel.is_set())
        self.settle(lambda: not self.window._busy)

    def test_poll_interval_presets_and_clamp(self):
        # 自动读取周期只能选预设（下拉不可编辑），最快 0.1 s（100 ms）。
        self.assertFalse(self.window.interval.isEditable())
        presets = [self.window.interval.itemData(i) for i in range(self.window.interval.count())]
        self.assertEqual(min(presets), 100)   # “0.1 s” 是可选预设
        for i, expected in enumerate(presets):
            with self.subTest(preset=expected):
                self.window.interval.setCurrentIndex(i)
                self.assertEqual(self.window._poll_interval_ms(), expected)
        # 旧配置里的自定义周期回填到最接近的预设（300 → 200）。
        cfg = self.window.cfg
        cfg["poll_interval"] = 300
        self.window._load_config_fields()
        self.assertEqual(self.window._poll_interval_ms(), 200)
        cfg["poll_interval"] = 7000
        self.window._load_config_fields()
        self.assertEqual(self.window._poll_interval_ms(), 5000)

    def test_import_component_updates_runtime_definition(self):
        from PySide6.QtWidgets import QMessageBox
        folder = Path(self.temp.name) / "ec_1do"
        folder.mkdir()
        (folder / "ps_rw_pl_reg_1do.sv").write_text(
            "module d(input wr_task_vld, input [8:0] wr_task_addr, input [31:0] i_st_wr_data,"
            "input [8:0] rd_addr_d2, output reg [31:0] o_st_rd_data);\n"
            "always @(*) case (rd_addr_d2) `EC_ID: o_st_rd_data = 32'd1;"
            " default: o_st_rd_data = 32'd0; endcase\nendmodule\n", encoding="utf-8")
        include = Path(self.temp.name) / "include_files"
        include.mkdir(exist_ok=True)
        (include / "reg_addr_pl.vh").write_text("`define EC_ID 9'h00C\n", encoding="utf-8")
        with patch("devmem_studio.window.QFileDialog.getExistingDirectory", return_value=str(folder)), \
                patch("devmem_studio.window.QMessageBox.question", return_value=QMessageBox.No):
            self.window.import_component()
        self.assertNotIn("imported_from", self.window.type_catalog["types"]["ec_1do"])
        with patch("devmem_studio.window.QFileDialog.getExistingDirectory", return_value=str(folder)), \
                patch("devmem_studio.window.QMessageBox.question", return_value=QMessageBox.Yes), \
                patch("devmem_studio.window.user_data_dir", return_value=Path(self.temp.name)):
            self.window.import_component()
        self.assertTrue((Path(self.temp.name) / "component_overrides" / "ec_1do.json").exists())
        entry = self.window.type_catalog["types"]["ec_1do"]
        self.assertEqual(entry["imported_from"], str(folder))
        self.assertEqual([item["name"] for item in entry["registers"]],
                         ["IRQ_REG1", "IRQ_REG2", "EC_ID"])
        # The imported table applies immediately to components of that type.
        self.select_and_wait(self.window.top_info["components"][1])
        self.assertEqual(len(self.window.regs), 3)

    def test_import_component_batch_scans_parent_directory(self):
        from PySide6.QtWidgets import QMessageBox
        folder = Path(self.temp.name) / "ec_1do"
        folder.mkdir()
        (folder / "ps_rw_pl_reg_1do.sv").write_text(
            "module d(input [8:0] rd_addr_d2, output reg [31:0] o_st_rd_data);\n"
            "always @(*) case (rd_addr_d2) `EC_ID: o_st_rd_data = 32'd1;"
            " default: o_st_rd_data = 32'd0; endcase\nendmodule\n", encoding="utf-8")
        include = Path(self.temp.name) / "include_files"
        include.mkdir(exist_ok=True)
        (include / "reg_addr_pl.vh").write_text("`define EC_ID 9'h00C\n", encoding="utf-8")
        with patch("devmem_studio.window.QFileDialog.getExistingDirectory",
                   return_value=str(Path(self.temp.name))), \
                patch("devmem_studio.window.QMessageBox.question",
                      return_value=QMessageBox.Yes) as question, \
                patch("devmem_studio.window.user_data_dir", return_value=Path(self.temp.name)):
            self.window.import_component()
        self.assertIn("解析到 3 个寄存器", question.call_args[0][2])
        self.assertIn(str(Path(self.temp.name)), question.call_args[0][2])
        self.assertIn("imported_from", self.window.type_catalog["types"]["ec_1do"])
        self.select_and_wait(self.window.top_info["components"][1])
        self.assertEqual(len(self.window.regs), 3)

    def test_startup_reload_of_saved_top_path(self):
        path = self.write_fixture_top()
        store = ConfigStore(Path(self.temp.name) / "reload.json")
        store.save({**store.load(), "top_path": str(path)})
        reloaded = MainWindow(store, persist=False)
        try:
            self.assertEqual(reloaded.component_tree.topLevelItemCount(), 3)
            self.assertIn("已重新加载 top", " ".join(message for _, _, message in reloaded.log_records_system))
            self.assertFalse(reloaded.component_mode)
        finally:
            reloaded.close()
            self.settle(lambda: not reloaded._busy and not reloaded.isVisible())
        missing = ConfigStore(Path(self.temp.name) / "missing.json")
        missing.save({**missing.load(), "top_path": str(Path(self.temp.name) / "gone.sv")})
        fresh = MainWindow(missing, persist=False)
        try:
            self.assertEqual(fresh.component_tree.topLevelItemCount(), 0)
            self.assertTrue(any("不存在" in message for _, _, message in fresh.log_records_system))
        finally:
            fresh.close()
            self.settle(lambda: not fresh._busy and not fresh.isVisible())


    def test_bit_upload_paste_file_path(self):
        # Paste a raw .bit path (text clipboard) → picked.
        bit = Path(self.temp.name) / "pasted.bit"
        bit.write_bytes(b"\x00" * 64)
        self.window.open_bit_upload()
        self.settle()
        dialog = self.window.bit_dialog
        self.assertEqual(dialog._local_path, None)
        QApplication.clipboard().setText(str(bit))
        dialog.paste_file()
        self.assertEqual(dialog._local_path, str(bit))
        self.assertEqual(dialog.path_label.text(), str(bit))

    def _normpath(self, p):
        # QUrl.toLocalFile() returns forward slashes; compare canonically.
        return str(Path(p)).lower()

    def test_bit_upload_paste_url_clipboard(self):
        # Copying a file in Explorer lands as a file:// URL; paste must accept it.
        bit = Path(self.temp.name) / "pasted_url.bit"
        bit.write_bytes(b"\x00" * 64)
        self.window.open_bit_upload()
        self.settle()
        dialog = self.window.bit_dialog
        QApplication.clipboard().setText(bit.as_uri())
        dialog.paste_file()
        self.assertEqual(self._normpath(dialog._local_path), self._normpath(bit))

    def test_bit_upload_paste_ignores_wrong_type(self):
        # Non-.bit content in the clipboard must not select anything (no modal blocking).
        self.window.open_bit_upload()
        self.settle()
        dialog = self.window.bit_dialog
        QApplication.clipboard().setText(str(Path("C:/Windows/notepad.exe")))
        dialog.paste_file()
        self.assertEqual(dialog._local_path, None)
        self.assertIn("剪贴板中没有", dialog.status.text())
        QApplication.clipboard().setText("")
        self.window.close()

    def test_bit_upload_resets_state_after_close(self):
        # Closing and reopening must forget the previously picked/dragged/pasted file.
        bit = Path(self.temp.name) / "pick.bit"
        bit.write_bytes(b"\x00" * 64)
        self.window.open_bit_upload()
        self.settle()
        dialog = self.window.bit_dialog
        dialog.set_path(str(bit))
        self.assertEqual(dialog._local_path, str(bit))
        dialog.close()
        self.settle(lambda: not dialog.isVisible())
        self.window.open_bit_upload()
        self.settle()
        dialog = self.window.bit_dialog
        self.assertEqual(dialog._local_path, None)
        self.assertEqual(dialog.path_label.text(), "未选择文件")
        self.assertEqual(dialog.status.text(), "")
        self.window.close()

    def test_bit_upload_set_path_rejects_non_bit(self):
        # Strict suffix check: a non-.bit file must be rejected with a warning.
        self.window.open_bit_upload()
        self.settle()
        dialog = self.window.bit_dialog
        bad = Path(self.temp.name) / "firmware.bin"
        bad.write_bytes(b"\x00" * 64)
        with patch("devmem_studio.dialogs.QMessageBox.warning") as warn:
            dialog.set_path(str(bad))
        self.assertEqual(dialog._local_path, None)
        self.assertEqual(dialog.path_label.text(), "未选择文件")
        self.assertEqual(warn.call_args.args[1], "文件类型错误")
        self.assertIn("选中文件不是bit文件", warn.call_args.args[2])
        # Case-insensitive: uppercase .BIT still accepted.
        good = Path(self.temp.name) / "fw.BIT"
        good.write_bytes(b"\x00" * 64)
        dialog.set_path(str(good))
        self.assertEqual(dialog._local_path, str(good))
        self.window.close()

    def test_bit_upload_start_upload_rejects_non_bit(self):
        self.window.open_bit_upload()
        self.settle()
        dialog = self.window.bit_dialog
        bad = Path(self.temp.name) / "firmware.bin"
        bad.write_bytes(b"\x00" * 64)
        dialog._local_path = str(bad)
        with patch("devmem_studio.dialogs.QMessageBox.warning") as warn:
            with patch.object(dialog, "upload_requested") as emitted:
                dialog.start_upload()
        self.assertEqual(warn.call_args.args[1], "文件类型错误")
        self.assertIn("选中文件不是bit文件", warn.call_args.args[2])
        emitted.emit.assert_not_called()
        self.window.close()

    def test_bit_upload_confirms_path_and_date_before_start(self):
        # Clicking 上传 asks 确认上传bit with the path + local file date; No cancels.
        bit = Path(self.temp.name) / "confirm.bit"
        bit.write_bytes(b"\x00" * 64)
        self.window.open_bit_upload()
        self.settle()
        dialog = self.window.bit_dialog
        dialog.set_path(str(bit))
        from datetime import datetime
        mtime = datetime.fromtimestamp(bit.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        with patch("devmem_studio.window.QMessageBox.question",
                   return_value=QMessageBox.No) as question, \
             patch.object(self.window.session, "upload_bitfile") as upload:
            dialog.start_upload()
        self.assertEqual(question.call_args.args[1], "确认上传bit")
        self.assertIn(str(bit), question.call_args.args[2])
        self.assertIn(mtime, question.call_args.args[2])
        self.assertIn("确定上传", question.call_args.args[2])
        upload.assert_not_called()
        self.assertFalse(self.window._busy)   # cancelled, no worker started
        self.window.close()

    def test_bit_upload_proceeds_after_confirm_yes(self):
        bit = Path(self.temp.name) / "confirm_yes.bit"
        bit.write_bytes(b"\x00" * 64)
        self.window.open_bit_upload()
        self.settle()
        dialog = self.window.bit_dialog
        dialog.set_path(str(bit))
        with patch("devmem_studio.window.QMessageBox.question",
                   return_value=QMessageBox.Yes), \
             patch.object(self.window.session, "upload_bitfile") as upload:
            dialog.start_upload()
            self.settle(lambda: not self.window._busy)
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0], str(bit))
        self.assertTrue(dialog.upload_button.isEnabled())
        self.window.close()


if __name__ == "__main__":
    unittest.main()
