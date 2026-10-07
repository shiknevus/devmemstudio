"""Executable-compatible offline acceptance; never reads production credentials or connects hardware."""
import csv
import io
import json
from pathlib import Path
import sys
import time
import traceback

import paramiko
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtWidgets import QMessageBox, QScrollArea, QLineEdit, QPlainTextEdit
from unittest.mock import patch
from . import bitpack, top_import
from .core import ConfigStore, DemoSession, HostKeyChangedError
from .dialogs import BatchDialog, HostKeyDialog
from .bitpack_dialog import BitPackDialog
from .window import MainWindow

FIXTURE_TOP = """// --- flow_comp_1 --A0001_验收轴---
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


def run_smoke(app, directory: Path):
    import os
    os.environ["DEVMEMSTUDIO_IGNORE_OVERRIDES"] = "1"   # acceptance runs against bundled data only
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    checks = []
    window = None
    report = {"frozen": bool(getattr(sys, "frozen", False)), "executable": sys.executable,
              "python": sys.version.split()[0], "paramiko": paramiko.__version__, "checks": checks}

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def settle(predicate=lambda: True, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            if predicate():
                for _ in range(4):
                    app.processEvents()
                return
            time.sleep(0.01)
        raise TimeoutError("Acceptance operation did not settle")

    def row_of(name):
        return next(index for index, reg in enumerate(window.regs) if reg["name"] == name)

    try:
        window = MainWindow(ConfigStore(directory / "test-config.json"), persist=False)
        window.resize(1540, 960)
        window.show()
        settle()
        report["device_pixel_ratio"] = window.devicePixelRatioF()
        report["initial_logical_size"] = [window.width(), window.height()]
        check(not window.connected and not window.read_all_button.isEnabled(), "Offline controls disabled")
        check(not window.component_mode and window.regs == [], "Empty register state before a component is chosen")
        check(window.bit_pack_button.isEnabled(), "Local bit packing is available offline")
        window.bit_pack_button.click()
        settle()
        pack_dialog = window.bit_pack_dialog
        check(pack_dialog.isVisible() and pack_dialog.parentWidget() is window,
              "Packing button opens a workbench-styled dialog offline")
        check(pack_dialog.start_button.objectName() == "primary" and pack_dialog.project.isEnabled(),
              "Packing dialog reuses the workbench primary button and input styling")
        window.bit_pack_button.click()
        check(window.bit_pack_dialog is pack_dialog, "Repeated packing clicks reuse the same styled dialog")
        pack_dialog.grab().save(str(directory / "bitpack-dialog.png"))
        pack_dialog.close()
        # Exercise the real bundled executable, including inside a copied frozen
        # workbench with Python and the original bitfile directory unavailable.
        import subprocess
        import zipfile
        with patch("devmem_studio.bitpack.user_data_dir", return_value=directory / "bitpack-cache"):
            packer = bitpack.prepare_bitpack_executable()
        check(packer.is_file() and packer.is_relative_to(directory), "Bundled packer is released to an independent cache")
        def run_packer(*arguments):
            result = subprocess.run([str(packer), *map(str, arguments)], cwd=directory,
                                    capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            check(result.returncode == 0, "Bundled packer CLI succeeds: " + str(arguments[0]))
            return result
        version = run_packer("--version").stdout.decode("utf-8").strip()
        check(version == "Bit_pack 2.0.1", "Bundled native packer version is correct")
        report["bitpack"] = version
        pack_directory = directory / "打包验收 空格"
        pack_directory.mkdir(exist_ok=True)
        source_bit = pack_directory / "源文件.bit"
        payload = bytes(range(256)) * 4
        source_bit.write_bytes(payload)
        run_packer("--project", "集成验收", "--input", source_bit, "--output", pack_directory)
        archives = list(pack_directory.glob("集成验收_bit_*.zip"))
        check(bool(archives), "Bundled packer creates a project release ZIP with Unicode paths")
        newest = max(archives, key=lambda path: path.stat().st_mtime_ns)
        with zipfile.ZipFile(newest) as archive:
            check(archive.namelist() == ["sunny_fpga.bit"] and archive.read("sunny_fpga.bit") == payload,
                  "Packed archive contains only the byte-identical sunny_fpga.bit")
        check(source_bit.read_bytes() == payload, "Packing preserves the original bit file")
        # Prove the real themed dialog + Qt worker + named-event CLI bridge also
        # functions inside the copied single-file EXE, not only in source tests.
        with patch("devmem_studio.bitpack.user_data_dir", return_value=directory / "bitpack-cache"):
            pack_dialog.set_source(source_bit)
            pack_dialog.project.setText("界面验收")
            pack_dialog.show()
            pack_dialog.start_button.click()
            check(pack_dialog.busy, "Styled packing dialog starts a real asynchronous engine job")
            settle(lambda: not pack_dialog.busy, timeout=30)
            check(pack_dialog.status.property("state") == "connected" and pack_dialog.progress.value() == 100,
                  "Styled packing dialog displays actual engine completion and verified progress")
            with zipfile.ZipFile(pack_dialog._result_path) as archive:
                check(archive.namelist() == ["sunny_fpga.bit"] and archive.read("sunny_fpga.bit") == payload,
                      "Styled dialog publishes a byte-identical archive through the bundled engine")
            check(pack_dialog._cancel is None and pack_dialog.open_output_button.isEnabled(),
                  "Styled dialog releases its cancellation event and enables output browsing after success")
            pack_dialog.grab().save(str(directory / "bitpack-success.png"))
            nested = pack_directory / "子目录"
            nested.mkdir(exist_ok=True)
            nested_bit = nested / "nested.BIT"
            nested_bit.write_bytes(b"nested fixture")
            pack_dialog.scan_directory(pack_directory)
            settle(lambda: not pack_dialog.busy, timeout=30)
            check(pack_dialog.source_combo.count() == 2, "Styled dialog recursively scans a chosen folder for bit files")
            index = pack_dialog.source_combo.findText(str(nested_bit))
            check(index >= 0, "Folder scan includes uppercase .BIT in nested directories")
            pack_dialog.output.setText(str(pack_directory))
            pack_dialog._output_edited(str(pack_directory))
            pack_dialog.source_combo.setCurrentIndex(index)
            pack_dialog.project.setText("目录界面验收")
            pack_dialog.start_button.click()
            settle(lambda: not pack_dialog.busy, timeout=30)
            check(pack_dialog.status.property("state") == "connected", "Folder selection packs the chosen bit with the real engine")
            with zipfile.ZipFile(pack_dialog._result_path) as archive:
                check(archive.read("sunny_fpga.bit") == b"nested fixture", "Folder-selected archive contains the selected file, not another bit")
            pack_dialog.nopack.setChecked(True)
            preferences = pack_dialog.preference_snapshot()
            check(window.cfg.get("bitpack_settings") == preferences,
                  "Project, copy-only mode and selected/output directories are passed to the main configuration")
            restored = BitPackDialog(window, directory, preferences)
            restored.show()
            settle(lambda: not restored.busy and restored.source_combo.count() == 2, timeout=30)
            check(restored.project.text() == "目录界面验收" and restored.output.text() == str(pack_directory)
                  and restored.source.text() == str(nested_bit), "New styled dialog restores project, directories and selected file")
            check(restored.nopack.isChecked() and not restored.project.isEnabled(),
                  "New styled dialog restores copy-only mode and keeps the remembered project disabled")
            pack_dialog.nopack.setChecked(False)
            check(window.cfg["bitpack_settings"]["nopack"] is False
                  and pack_dialog.project.text() == "目录界面验收" and pack_dialog.project.isEnabled(),
                  "Unchecking copy-only mode is remembered and re-enables the retained project")
            restored.close()
            restored.deleteLater()
            pack_dialog.grab().save(str(directory / "bitpack-folder.png"))
            pack_dialog.close()
        window.grab().save(str(directory / "offline.png"))
        window.session = DemoSession(window._log_emit("ssh"))
        window.session.connect()
        window._set_connection(True)
        settle()
        check(window.connected, "Simulated device session for offline acceptance")
        catalog = top_import.load_type_catalog()
        check(catalog is not None and "ec_slv_pul_axis" in catalog["types"],
              "Bundled component catalog ships exact per-type register tables")
        fixture = directory / "fixture-top.sv"
        fixture.write_text(FIXTURE_TOP, encoding="utf-8")
        check(window.load_top(fixture), "Import mix top populates the component directory")
        check(window.component_tree.topLevelItemCount() == 3, "Imported components grouped by module type")
        check(not window.component_mode, "Import alone does not select a component")
        window.read_all()
        check(any("选择组件" in text for _, _, text in window.log_records_system),
              "Reading without a component gives a hint in the system terminal")
        check(window.base_field.isEnabled() and window.base_field.text().lower() == "0xb0100000",
              "Base address stays editable and defaults sanely without components_param.vh")
        target = next(item for item in window.top_info["components"] if not item["disabled"]
                      and item["module_type"] == "ec_slv_pul_axis")
        window.select_component(target)
        settle(lambda: not window._busy)
        check(window.component_mode and window.cache_key.startswith("top:")
              and window.regs[0]["_address"] == 0xB0100000 + target["bias"], "Selecting a component pins its register address")
        check(all(reg["_value"] is not None for reg in window.visible_regs), "Selecting a component reads it once")
        check(window.read_count == len(window.visible_regs), "Read counter matches completed operations")
        # Runtime component re-import: a changed RTL folder updates the definition without a rebuild.
        bundled_entry = window.type_catalog["types"]["ec_slv_pul_axis"]
        comp_folder = directory / "ec_slv_pul_axis"
        comp_folder.mkdir(exist_ok=True)
        (directory / "include_files").mkdir(exist_ok=True)
        (directory / "include_files" / "reg_addr_pl.vh").write_text("`define EC_ID 9'h00C\n", encoding="utf-8")
        (comp_folder / "ps_rw_pl_reg_slv_pul_axis.sv").write_text(
            "module d(input wr_task_vld, input [8:0] wr_task_addr, input [31:0] i_st_wr_data,"
            "input [8:0] rd_addr_d2, output reg [31:0] o_st_rd_data);\n"
            "always @(*) case (rd_addr_d2) `EC_ID: o_st_rd_data = 32'd1;"
            " default: o_st_rd_data = 32'd0; endcase\nendmodule\n", encoding="utf-8")
        with patch("devmem_studio.window.QFileDialog.getExistingDirectory", return_value=str(directory)), \
                patch("devmem_studio.window.QMessageBox.question", return_value=QMessageBox.Yes), \
                patch("devmem_studio.window.user_data_dir", return_value=directory):
            window.import_component()
        imported = window.type_catalog["types"]["ec_slv_pul_axis"]
        check("imported_from" in imported and
              [item["name"] for item in imported["registers"]] == ["IRQ_REG1", "IRQ_REG2", "EC_ID"],
              "Runtime component import re-parses a changed RTL folder")
        window.select_component(target)
        settle(lambda: not window._busy)
        check(len(window.regs) == 3, "Imported definition applies to the selected component immediately")
        os.environ.pop("DEVMEMSTUDIO_IGNORE_OVERRIDES", None)   # verify the override reloads
        with patch("devmem_studio.top_import.user_data_dir", return_value=directory):
            window.type_catalog = top_import.load_type_catalog()
        os.environ["DEVMEMSTUDIO_IGNORE_OVERRIDES"] = "1"
        check("imported_from" in window.type_catalog["types"]["ec_slv_pul_axis"],
              "Imported definitions persist and reload with the catalog")
        window.type_catalog["types"]["ec_slv_pul_axis"] = bundled_entry   # continue with the full table
        window._last_context = None
        window.select_component(target)
        settle(lambda: not window._busy)
        for view in top_import.VIEW_ORDER:
            window.view_buttons[view].click()
            settle(lambda: not window._busy)
            check(bool(window.visible_regs) and all(top_import.reg_in_view(reg, view) for reg in window.visible_regs),
                  f"View {view} slices the exact register table")
        check(window.read_count > 0, "Switching views reads the visible registers")
        window.view_buttons["all"].click()
        settle(lambda: not window._busy)
        for name, expected in (
            ("IRQ_REG1", {"ec_id": "5", "sc_id": "2", "r_a_bhv_id": "1"}),
            ("IRQ_REG2", {"r_a_tx_id": "8", "r_a_alm_num": "0"}),
            ("A_TX_RSULT_RPT", {"ack_beh_id": "3", "ack_tx_id": "8", "ack_tx_result": "0x01", "ack_ps_alart_num": "0"}),
            ("B_TX_RSULT_RPT", {"ack_beh_id": "2", "ack_tx_id": "4", "ack_tx_result": "0x00", "ack_ps_alart_num": "0"}),
            ("C_TX_RSULT_RPT", {"ack_beh_id": "1", "ack_tx_id": "2", "ack_tx_result": "0x00", "ack_ps_alart_num": "0"}),
        ):
            window.table.selectRow(row_of(name))
            window.bit_mode.click()
            settle()
            check(window.decoded_view.isVisible() and
                  {key: cell.text() for key, cell in window.decoded_view.field_values.items()} == expected,
                  f"{name} decoded fields use specified names and display radix")
            if name in ("IRQ_REG1", "IRQ_REG2"):
                window.grab().save(str(directory / f"{name.lower()}-fields.png"))
        window.table.selectRow(row_of("A_TX_RSULT_RPT"))
        window.bit_mode.click()
        settle()
        window.grab().save(str(directory / "workbench.png"))
        window.view_buttons["task_a"].click()
        settle(lambda: not window._busy)
        window.table.selectRow(row_of("A_BHV_ID"))
        window.write_mode.click()   # 选中默认在“解析与位状态”，预设（写设置）需切页
        settle()
        check(window.preset_combo.isVisible() and window.preset_combo.itemText(1).strip() == "home   (1)",
              "Behavior presets harvested from RTL populate the trigger register")
        window.view_buttons["debug"].click()
        settle(lambda: not window._busy)
        debug_row = row_of("DEBUG_REG1")
        window.session.memory[window.regs[debug_row]["_address"]] = 0x05030205
        window.table.selectRow(debug_row)
        window.read_selected()
        settle(lambda: not window._busy)
        check({key: cell.text() for key, cell in window.decoded_view.field_values.items()} ==
              {"前第3拍": "5", "前第2拍": "3", "前第1拍": "2", "当前": "5"},
              "DEBUG_REG state history decodes each byte")
        window.grab().save(str(directory / "debug-decode.png"))
        window.view_buttons["all"].click()
        settle(lambda: not window._busy)
        window.search.setText("IRQ_REG1")
        settle()
        check(sum(not window.table.isRowHidden(i) for i in range(len(window.regs))) == 1, "Search by register name")
        window.search.setText("no_such_register")
        settle()
        check(window.empty_label.isVisible() and not window.inspector.isEnabled(), "Search empty state")
        window.search.clear()
        settle()
        window.access_filter.setCurrentIndex(1)
        settle()
        check(all(r.get("readonly") for r in window.visible_regs), "Read-only filter")
        check(not window.editor_panel.isVisible(), "Read-only write controls hidden")
        window.access_filter.setCurrentIndex(0)
        settle()
        window.table.selectRow(row_of("A_TX_OT"))
        check(window.format_combo.currentData() == "H", "Write format defaults to HEX")
        window.write_input.setText("42")
        window.format_combo.setCurrentIndex(1)
        check(window.write_input.text() == "42" and window.selected["_fmt"] == "D", "Format switch keeps text")
        window.format_combo.setCurrentIndex(0)
        window.write_input.setText("2A")
        window.write_button.click()
        settle(lambda: not window._busy)
        check(window.selected["_value"] == 42 and window.write_count == 1, "Write and measured readback")
        window.write_input.setText("invalid")
        check(not window.write_button.isEnabled(), "Invalid write value blocked")
        window.write_input.setText("FFFFFFFFF")
        check(not window.write_button.isEnabled(), "Overflow blocked")
        window.write_input.setText("2A")
        writable = [r for r in window.regs if not r.get("readonly")]
        for reg in writable:
            window._prepare_write(reg)
        dialog = BatchDialog(writable, window)
        dialog.show()
        settle()
        check(bool(dialog.selected()), "Batch preview contains normal writable registers")
        dialog.grab().save(str(directory / "batch-preview.png"))
        dialog.close()
        old_key = window.cache_key
        window.select_component(next(item for item in window.top_info["components"]
                                     if item["module_type"] == "ec_1do"))
        settle(lambda: not window._busy and window.cache_key != old_key)
        check(window.cache_key != old_key and all(r["_value"] is not None for r in window.regs),
              "Component switch refreshes measurements for the new component")
        window.select_component(target)
        settle(lambda: not window._busy and window.cache_key == old_key)
        check(next(r for r in window.regs if r["name"] == "A_TX_OT")["_draft"] == "2A",
              "Per-component write cache restored")
        window.send_command("devmem 0xb0100800")
        settle(lambda: not window._busy)
        check(any(message == "devmem 0xb0100800" for _, _, message in window.log_records_ssh),
              "Terminal command read (devmem 0xb0100800) runs over SSH")
        before = window.read_count
        window.poll_check.setChecked(True)
        settle(lambda: window.read_count > before and not window._busy)
        window.poll_check.setChecked(False)
        check(window.read_count > before, "Automatic polling completes without overlapping jobs")
        window.read_all()
        window.cancel_task()
        settle(lambda: not window._busy)
        check(window.cancel.is_set(), "Batch cancellation")
        # No auto-jump: the console stays on system after connecting; sunny.log
        # auto-tails in the background regardless of the visible source.
        check(window.console_source == "system" and window._stream_state == "running",
              "Console stays on system after connect while sunny.log auto-tails")
        window._set_console_source("log")
        settle(lambda: "DEMO" in window.console.toPlainText())
        check(any(level == "CMD" and message == "tail -f /run/media/sda/sunny.log"
                  for _, level, message in window.log_records), "Print logs sends the exact tail command automatically")
        check(not window._busy and window.read_all_button.isEnabled(), "Log startup leaves register controls available")
        window.read_selected()
        settle(lambda: not window._busy)
        check("DEMO" in window.console.toPlainText(), "Concurrent register reads and board log stream")
        check(window.log_wrap.isChecked() and window.console.lineWrapMode() == QPlainTextEdit.WidgetWidth,
              "Log wrapping is enabled by default")
        window.log_search.setText("axis")
        window._find_in_log(forward=True)
        settle(lambda: window.console.textCursor().hasSelection())
        check("axis" in window.console.textCursor().selectedText(), "Log search finds and selects the query")
        first_match = window.console.textCursor().selectionStart()
        window._find_in_log(forward=True)
        settle(lambda: window.console.textCursor().selectionStart() > first_match)
        check(window.console.textCursor().selectionStart() > first_match, "Log search jumps to next match")
        window._find_in_log(forward=False)
        check(window.console.textCursor().selectionStart() == first_match, "Log search jumps to previous match")
        long_line = "INFO [DEMO] axis 长日志自动换行演示 · 连续采样数据 payload=0x" + "A17B23CC" * 40 + " · 查询期间仍在接收日志"
        window._append_log_stream(long_line + "\n")
        settle(lambda: not window._busy)
        # Locate the just-appended long block by its payload marker; its block number
        # shifts as setMaximumBlockCount trims, so search rather than index.
        doc = window.console.document()
        long_block = doc.begin()
        while long_block.isValid() and "payload=0x" not in long_block.text():
            long_block = long_block.next()
        check(long_block.isValid() and long_block.layout().lineCount() > 1
              and window.console.horizontalScrollBar().maximum() == 0,
              "Long unbroken payload wraps without a horizontal scrollbar")
        window.log_wrap.setChecked(False)
        settle()
        check(long_block.layout().lineCount() == 1 and window.console.horizontalScrollBar().maximum() > 0,
              "Disabling log wrap exposes the horizontal scrollbar")
        window.log_wrap.setChecked(True)
        window.console.grab().save(str(directory / "board-log.png"))
        window._stop_stream()
        settle(lambda: window._stream_state == "stopped")
        check(window.session.alive and window.read_all_button.isEnabled(), "Closing logs preserves the register session")
        data = list(csv.reader(io.StringIO(window.snapshot_csv())))
        check(len(data) == len(window.regs) + 1 and data[1][0] == "离线演示", "CSV snapshot with honest demo provenance")
        (directory / "snapshot.csv").write_text(window.snapshot_csv(), encoding="utf-8-sig")
        window.resize(1280, 800)
        settle()
        window.write_mode.click()   # 默认选中落在“解析与位状态”，写编辑器需切到“写入设置”页
        settle()
        editor_position = window.write_input.mapTo(window.inspector_scroll.viewport(), window.write_input.rect().topLeft())
        editor_rect = window.write_input.rect().translated(editor_position)
        check(window.inspector_scroll.viewport().rect().contains(editor_rect), "Write editor is revealed automatically in compact layout")
        check(window.inspector.rect().contains(window.reg_address.mapTo(window.inspector, window.reg_address.rect().center())),
              "Target address remains fixed above the scrolling editor")
        window.grab().save(str(directory / "compact.png"))
        check(window.width() == 1280, "Compact desktop layout")
        check(all(control.isVisible() and control.parentWidget().rect().contains(control.geometry())
                  and control.width() >= control.minimumSizeHint().width() for control in
                  (window.bit_pack_button, window.bit_upload_button, window.bit_rollback_button,
                   window.log_download_button, window.reboot_button)),
              "Local packer and firmware actions remain visible at compact size")
        check(all(abs(control.geometry().center().y() - window.log_search.geometry().center().y()) <= 2
                  for control in (window.bit_pack_button, window.bit_upload_button, window.bit_rollback_button,
                                  window.log_download_button, window.reboot_button)),
              "Firmware actions and log search share a single toolbar row")
        check(not window.ssh_section.is_expanded() and not window.serial_section.is_expanded()
              and window.connect_button.isVisible() and window.serial_connect_button.isVisible()
              and window.component_tree.isVisible(),
              "Compact mode folds SSH/serial to one line each, keeping connect buttons and the component tree visible")
        sidebar = window.sidebar_scroll
        check(not isinstance(sidebar, QScrollArea) and not sidebar.findChildren(QScrollArea),
              "Sidebar has no outer or nested vertical scroll area")
        check(sidebar.width() == 300 and window.side_split.sizes()[0] == 300
              and window.workspace.x() == 300 + window.side_split.handleWidth()
              and not window.side_split.handle(1).isEnabled(),
              "Compact sidebar width is fixed without a wasted gutter while the component tree remains scrollable")
        check(sidebar.widget().width() <= sidebar.viewport().width(), "Sidebar content fits its horizontal viewport")
        window.serial_section.toggle.click()
        settle()
        check(window.serial_section.is_expanded() and window.serial_port.isVisible()
              and not window.ssh_section.is_expanded() and window.component_tree.isVisible()
              and sidebar.widget().rect().contains(window.component_tree.geometry()),
              "Opening one channel shows its fields inline without hiding the component tree")
        window.sidebar_scroll.grab().save(str(directory / "connection-settings.png"))
        window.serial_section.toggle.click()
        settle()
        check(not window.serial_section.is_expanded() and not window.serial_port.isVisible(),
              "Folding the channel restores the tree-only rail")
        check(window.write_button.isVisible() and window.inspector.rect().contains(
            window.write_button.mapTo(window.inspector, window.write_button.rect().center())), "Inspector action remains visible at compact size")
        window.table.selectRow(row_of("A_TX_RSULT_RPT"))
        window.bit_mode.click()
        settle()
        decoded_rect = window.decoded_view.rect().translated(window.decoded_view.mapTo(window.inspector_scroll.viewport(),
                                                                                       window.decoded_view.rect().topLeft()))
        check(window.inspector_scroll.viewport().rect().contains(decoded_rect), "All report fields remain visible in the compact inspector")
        check(all(cell.fontMetrics().horizontalAdvance(cell.text()) <= cell.width() for cells in window.decoded_view.rows for cell in cells),
              "Decoded field names and values fit without clipping")
        window.grab().save(str(directory / "rpt-fields-compact.png"))
        window.view_buttons["basic"].click()
        settle(lambda: not window._busy)
        window.read_all_button.click()
        settle(lambda: not window._busy)
        check(bool(window.visible_regs) and all(reg["name"] in top_import.VIEW_SETS["basic"]
                                                for reg in window.visible_regs),
              "Basic view shows the common component header")
        window.table.selectRow(row_of("EC_ID"))
        window.format_combo.setCurrentIndex(0)   # HEX，否则 "2A" 按 DEC 解析无效
        window.write_input.setText("2A")
        window.write_button.click()
        settle(lambda: not window._busy)
        check(window.selected["name"] == "EC_ID" and window.selected["_value"] == 42,
              "Header register writes and reads back through the normal workbench")
        (directory / "basic-snapshot.csv").write_text(window.snapshot_csv(), encoding="utf-8-sig")
        window.view_buttons["all"].click()
        window.resize(1540, 960)
        settle()
        window.grab().save(str(directory / "basic-component.png"))
        window.read_all_button.click()
        settle(lambda: not window._busy)
        window.disconnect()
        check(not window.connected and not window.poll_check.isChecked(), "Disconnect stops polling and disables operations")
        check(list(csv.reader(io.StringIO(window.snapshot_csv())))[1][0] == "离线演示", "Disconnected demo snapshot preserves data provenance")
        check(not (directory / "test-config.json").exists(), "Acceptance does not save credentials or production settings")
        change = HostKeyChangedError("192.0.2.10", 22, paramiko.RSAKey.generate(2048),
                                     paramiko.RSAKey.generate(2048))
        trust_dialog = HostKeyDialog(change, window)
        trust_dialog.setWindowTitle("确认板卡主机密钥 · 离线示例")
        trust_dialog.show()
        settle()
        fingerprints = trust_dialog.findChildren(QLineEdit)
        check([field.text() for field in fingerprints] == [change.old_fingerprint, change.new_fingerprint]
              and all(field.isReadOnly() for field in fingerprints), "Host key review shows both exact fingerprints")
        check(trust_dialog.cancel_button.isDefault() and not trust_dialog.confirm_button.isDefault(),
              "Host key review defaults to cancellation")
        check(all(field.fontMetrics().horizontalAdvance(field.text()) < field.contentsRect().width() - 24
                  for field in fingerprints), "Both fingerprints fit at the current DPI")
        trust_dialog.grab().save(str(directory / "host-key-change.png"))
        trust_dialog.cancel_button.click()
        settle()
        window.close()
        settle()
        check(not window.isVisible(), "Clean shutdown")
        report["passed"] = True
    except Exception:
        report["passed"] = False
        report["error"] = traceback.format_exc()
        if window:
            window.cancel_task()
            window.session.close()
            try:
                settle(lambda: not window._busy)
                window.grab().save(str(directory / "failure.png"))
                window.close()
            except Exception:
                pass
    (directory / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if report["passed"] else 1
