# -*- coding: utf-8 -*-
import re
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QTimer, QUrl, QThreadPool
from PySide6.QtGui import QFont, QKeySequence, QShortcut, QGuiApplication
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
                               QHeaderView, QAbstractItemView, QDialogButtonBox, QPlainTextEdit,
                               QFileDialog, QMessageBox, QTextBrowser, QLineEdit, QCheckBox, QSpinBox,
                               QPushButton, QProgressBar, QListWidget, QListWidgetItem, QComboBox, QSizePolicy)
from . import file_scan
from .core import write_command, DEFAULT_BIT_DIR
from .widgets import label, button, row, ComboBox, Worker
from .theme import icon
from .log_view import LogView, LogSearchEdit


class BatchDialog(QDialog):
    def __init__(self, registers, parent=None):
        super().__init__(parent)
        self.setWindowTitle("批量写入预览")
        self.resize(800, 560)
        self.registers = registers
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        layout.addWidget(label("确认本次写入", "title"))
        note = label("逐项核对目标地址与写入值。动作和伺服按钮默认不勾选；执行中可停止后续写入。", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(len(registers), 5)
        self.table.setHorizontalHeaderLabels(["写入", "寄存器", "目标地址", "位宽", "写入值 · HEX"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(37)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        alignments = (Qt.AlignHCenter, Qt.AlignLeft, Qt.AlignRight, Qt.AlignHCenter, Qt.AlignRight)
        for col, alignment in enumerate(alignments):
            self.table.horizontalHeaderItem(col).setTextAlignment(alignment | Qt.AlignVCenter)
        for index, reg in enumerate(registers):
            selected = QTableWidgetItem()
            selected.setTextAlignment(Qt.AlignCenter)
            selected.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            selected.setCheckState(Qt.Unchecked if reg.get("action") or reg.get("buttons") else Qt.Checked)
            self.table.setItem(index, 0, selected)
            for col, text in enumerate((reg["name"], f'0x{reg["_address"]:08X}', str(reg["_access_width"]),
                                        f'0x{reg["_planned_value"]:X}'), 1):
                item = QTableWidgetItem(text)
                item.setTextAlignment(self.table.horizontalHeaderItem(col).textAlignment())
                if col >= 2:
                    item.setFont(QFont("Consolas", 10))
                self.table.setItem(index, col, item)
        layout.addWidget(self.table)
        self.count = label("", "muted")
        self.table.itemChanged.connect(self.update_count)
        self.confirm = button("执行写入", self.accept, "primary", "write")
        self.confirm.setMinimumWidth(120)
        layout.addLayout(row(self.count, 1, button("取消", self.reject), self.confirm))
        self.update_count()

    def selected(self):
        return [reg for i, reg in enumerate(self.registers) if self.table.item(i, 0).checkState() == Qt.Checked]

    def update_count(self):
        self.count.setText(f"已选择 {len(self.selected())} / {len(self.registers)} 项 · 按表格顺序执行")
        self.confirm.setEnabled(bool(self.selected()))


class BoardLogDialog(QDialog):
    start_requested = Signal(str)
    stop_requested = Signal()

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("打印日志")
        # A full window: minimize/maximize enabled, with its own taskbar entry.
        self.setWindowFlags(Qt.Window)
        self.setWindowModality(Qt.NonModal)
        self._connected = True
        self._stream_state = "stopped"
        self.resize(1080, 650)
        self.setMinimumSize(900, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        legend = label('<span style="color:#B6404D">● 错误</span>　'
                       '<span style="color:#9A681D">● 告警</span>　'
                       '<span style="color:#177E67">● 成功</span>　'
                       '<span style="color:#718397">● 调试</span>', "muted")
        layout.addLayout(row(label("板端实时日志", "sectionTitle"), 1, legend,
                             button("保存日志", self.save_log, "flat", "export")))
        self.path = QLineEdit(path)
        self.path.setPlaceholderText("板端日志的绝对路径")
        self.path.setObjectName("mono")
        self.start_button = button("开始打印", lambda: self.start_requested.emit(self.path.text()), "primary", "play")
        self.stop_button = button("停止打印", self.stop_requested.emit, None, "stop")
        self.stop_button.setEnabled(False)
        layout.addLayout(row(label("tail -f", "mono"), self.path, self.start_button, self.stop_button))
        self.search = LogSearchEdit()
        self.search.setPlaceholderText("查找日志内容 · Ctrl+F")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("search", size=16), QLineEdit.LeadingPosition)
        self.case_sensitive = QCheckBox("区分大小写")
        self.match_count = label("0 / 0", "mono")
        self.match_count.setMinimumWidth(82)
        self.match_count.setAlignment(Qt.AlignCenter)
        self.previous_button = button("上一处", lambda: self.find_match(False), None, "up")
        self.next_button = button("下一处", lambda: self.find_match(True), None, "down")
        self.previous_button.setToolTip("上一处匹配 · Shift+F3 / Shift+Enter")
        self.next_button.setToolTip("下一处匹配 · F3 / Enter；到末尾后循环查找")
        layout.addLayout(row(self.search, self.match_count, self.case_sensitive, self.previous_button, self.next_button))
        self.text = LogView()
        layout.addWidget(self.text, 1)
        self.state = label("独立日志通道 · 读取寄存器时可继续监听", "muted")
        self.follow = QCheckBox("自动跟随")
        self.follow.setChecked(True)
        self.follow.setToolTip("查找、跳转或向上浏览会暂停跟随，日志继续接收。")
        self.latest_button = button("回到最新", self.go_latest, "flat", "down")
        self.wrap = QCheckBox("自动换行")
        self.wrap.setChecked(True)
        self.wrap.setToolTip("长日志按窗口宽度折行显示；原始行号、复制和保存的内容不变。")
        self.show_line_numbers = QCheckBox("显示行号")
        self.show_line_numbers.setChecked(True)
        self.show_line_numbers.setToolTip("在左侧显示当前缓存的原始行号，与 Ctrl+G 跳转一致。")
        self.line_number = QSpinBox()
        self.line_number.setRange(1, 1)
        self.line_number.setFixedWidth(74)
        self.line_number.setToolTip("当前缓存中的行号 · Ctrl+G；最多保留最近 5000 行。")
        self.line_count = label("/ 1 行", "muted")
        self.jump_button = button("跳转", self.jump_to_line, "flat")
        layout.addLayout(row(self.follow, self.latest_button, self.wrap, self.show_line_numbers, 1, label("跳至行", "muted"),
                             self.line_number, self.line_count, self.jump_button, button("清空", self.text.clear, "flat")))
        layout.addLayout(row(self.state, 1, label("F3 下一处 · Shift+F3 上一处 · Ctrl+G 跳转行", "muted")))
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(150)
        self.search_timer.timeout.connect(self.apply_search)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        self.case_sensitive.toggled.connect(lambda: self.search_timer.start())
        self.search.next_requested.connect(lambda: self.find_match(True))
        self.search.previous_requested.connect(lambda: self.find_match(False))
        self.search.leave_requested.connect(self.text.setFocus)
        self.text.matches_changed.connect(self.update_matches)
        self.text.location_changed.connect(self.update_location)
        self.text.browse_requested.connect(lambda: self.follow.setChecked(False))
        self.follow.toggled.connect(lambda checked: self.go_latest() if checked else None)
        self.wrap.toggled.connect(self.update_log_layout)
        self.show_line_numbers.toggled.connect(self.update_log_layout)
        for sequence, callback in (("Ctrl+F", self.focus_search), ("F3", lambda: self.find_match(True)),
                                   ("Shift+F3", lambda: self.find_match(False)), ("Ctrl+G", self.focus_line)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
        # Enter in the find field must never start or stop the SSH stream.
        for item in self.findChildren(QPushButton):
            item.setAutoDefault(False)
        self.update_matches(0, 0)

    def focus_search(self):
        selected = self.text.textCursor().selectedText()
        if selected and "\u2029" not in selected:
            self.search.setText(selected)
        self.search.setFocus()
        self.search.selectAll()

    def focus_line(self):
        self.line_number.setFocus()
        self.line_number.selectAll()

    def apply_search(self, navigate=True):
        self.search_timer.stop()
        self.text.set_search(self.search.text(), self.case_sensitive.isChecked())
        if navigate and self.search.text():
            self.text.find_match(include_current=True)

    def find_match(self, forward=True):
        changed = self.search_timer.isActive()
        if changed:
            self.apply_search(navigate=False)
        self.text.find_match(forward, include_current=changed)

    def update_matches(self, current, total):
        self.match_count.setText(f"{current} / {total}")
        self.match_count.setToolTip("当前匹配 / 匹配总数" if total else "未找到匹配" if self.search.text() else "输入关键词查找")
        self.previous_button.setEnabled(total > 0)
        self.next_button.setEnabled(total > 0)

    def update_location(self, current, total):
        self.line_number.setMaximum(total)
        if not self.line_number.hasFocus():
            self.line_number.setValue(current)
        self.line_count.setText(f"/ {total} 行")

    def jump_to_line(self):
        self.text.jump_to_line(self.line_number.value())
        self.text.setFocus()

    def go_latest(self):
        self.follow.setChecked(True)
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def update_log_layout(self):
        self.text.set_wrapping(self.wrap.isChecked())
        self.text.set_line_numbers_visible(self.show_line_numbers.isChecked())
        if self.follow.isChecked():
            self.go_latest()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.line_number.hasFocus():
            self.jump_to_line()
            event.accept()
        else:
            super().keyPressEvent(event)

    def set_streaming(self, active, starting=False):
        self._stream_state = "starting" if starting else "running" if active else "stopped"
        busy = active or starting
        self.start_button.setEnabled(self._connected and not busy)
        self.start_button.setText("正在启动…" if starting else "打印中" if active else "开始打印")
        self.path.setEnabled(self._connected and not busy)
        self.stop_button.setEnabled(self._connected and busy)
        self.state.setText("连接已断开" if not self._connected else "正在启动 · 主窗口可继续读写" if starting else
                           "● 正在打印 · 主窗口可继续读写" if active else "已停止 · 可重新开始打印")

    def set_connected(self, connected):
        self._connected = connected
        self.set_streaming(connected and self._stream_state == "running",
                           connected and self._stream_state == "starting")

    def append(self, text):
        self.text.append_data(text, self.follow.isChecked())

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存板端日志", "board-log.txt", "文本日志 (*.txt)")
        if path:
            try:
                Path(path).write_text(self.text.toPlainText(), encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self, "保存失败", str(exc))

    def closeEvent(self, event):
        self.stop_requested.emit()
        event.accept()


class HostKeyDialog(QDialog):
    def __init__(self, change, parent=None):
        super().__init__(parent)
        self.setWindowTitle("确认板卡主机密钥")
        self.setModal(True)
        self.resize(740, 360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(label("板卡身份已变化", "title"))
        explanation = label(f"{change.hostname}:{change.port} 返回的 SSH 主机密钥与上次记录不一致。\n"
                            "板卡重启后重新生成密钥可能导致此提示。请核对设备地址与指纹。", "muted")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        for caption, key, fingerprint in (("上次记录", change.expected_key, change.old_fingerprint),
                                           ("本次连接", change.key, change.new_fingerprint)):
            layout.addWidget(label(f"{caption} · {key.get_name()}", "muted"))
            value = QLineEdit(fingerprint)
            value.setObjectName("mono")
            value.setReadOnly(True)
            value.setCursorPosition(0)
            layout.addWidget(value)
        note = label("确认是刚重启的目标板卡后，可更新该设备的记录并重新连接。原记录会自动备份。", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.cancel_button = button("取消", self.reject)
        self.cancel_button.setDefault(True)
        self.confirm_button = button("信任新密钥并重连", self.accept, "primary", "connect")
        self.confirm_button.setAutoDefault(False)
        layout.addLayout(row(1, self.cancel_button, self.confirm_button))
        self.cancel_button.setFocus()


class RemoteDirCombo(ComboBox):
    """Editable board-directory picker: remembered directories, the default and
    mounts found on the board; `committed` fires when a choice is settled."""
    committed = Signal(str)

    def __init__(self, directories=(), current=DEFAULT_BIT_DIR, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setObjectName("mono")
        self.setAccessibleName("板端目录")
        self.setMinimumContentsLength(18)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.lineEdit().setPlaceholderText(DEFAULT_BIT_DIR)
        self.set_directories(directories, current)
        self.activated.connect(lambda _index: self.committed.emit(self.path_text()))
        self.lineEdit().editingFinished.connect(lambda: self.committed.emit(self.path_text()))

    def path_text(self):
        return self.currentText().strip() or DEFAULT_BIT_DIR

    def set_directories(self, directories, current=None):
        current = current or self.path_text()
        self.blockSignals(True)
        self.clear()
        for directory in dict.fromkeys([*directories, DEFAULT_BIT_DIR]):
            self.addItem(directory)
        index = self.findText(current)
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            self.setEditText(current)
        self.blockSignals(False)
        self.setToolTip(f"板端目录：{current}（可下拉选择或直接输入，自动记住上次使用的目录）")

    def merge_directories(self, found):
        """Add directories discovered on the board without touching the current choice."""
        known = [self.itemText(index) for index in range(self.count())]
        extra = [directory for directory in found if directory not in known]
        if extra:
            self.set_directories(known + extra, self.path_text())


class FileChoiceDialog(QDialog):
    """Pick one of several files found under a folder; the first row is preselected."""

    def __init__(self, title, hint, root, rows, headers, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(860, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(label(title, "title"))
        note = label(hint, "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(len(rows), 1 + len(headers))
        self.table.setTextElideMode(Qt.ElideMiddle)   # keep the file name visible on deep paths
        self.table.setHorizontalHeaderLabels(["文件", *headers])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 1 + len(headers)):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        for index, (path, cells) in enumerate(rows):
            try:
                shown = str(Path(path).relative_to(root))
            except ValueError:
                shown = str(path)
            item = QTableWidgetItem(shown)
            item.setData(Qt.UserRole, str(path))
            item.setToolTip(str(path))
            self.table.setItem(index, 0, item)
            for column, text in enumerate(cells, 1):
                self.table.setItem(index, column, QTableWidgetItem(text))
        if rows:
            self.table.selectRow(0)
        self.table.doubleClicked.connect(lambda _index: self.accept())
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("选择")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_path(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None


def file_time(mtime):
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


class BitUploadDialog(QDialog):
    """Pick (file or folder scan), drop or paste a .bit file and send it to the board as sunny_fpga.bit."""
    upload_requested = Signal(str, str)  # local path, remote dir
    remote_dir_changed = Signal(str)
    local_dir_changed = Signal(str)

    def __init__(self, parent=None, remote_dirs=(), remote_dir=DEFAULT_BIT_DIR, local_dir=""):
        super().__init__(parent)
        self.setWindowTitle("上传bit file")
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self.setWindowModality(Qt.NonModal)
        self.setMinimumSize(600, 260)
        self.setAcceptDrops(True)
        self._local_path = None
        self._local_dir = local_dir
        self._scan_worker = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(label("选择、拖入或粘贴 .bit 文件", "title"))
        hint = label("可直接选文件，或选文件夹自动查找其下（含子目录）的 .bit，多个时列表选择。新文件先完整上传到所选板端目录，"
                     "再把原 sunny_fpga.bit 重命名为 sunny_fpga.bit_时间戳 备份并换入；传输失败时原文件不受影响。", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.path_label = label("未选择文件", "mono")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)
        self.choose_button = button("选择文件…", self.choose_file, "flat", "export")
        self.folder_button = button("选择文件夹…", self.choose_folder, "flat", "folder")
        paste = button("粘贴", self.paste_file, "flat", "export")
        layout.addLayout(row(self.choose_button, self.folder_button, paste, 1, spacing=8))
        self.remote_dir = RemoteDirCombo(remote_dirs, remote_dir)
        self.remote_dir.committed.connect(self.remote_dir_changed)
        layout.addLayout(row(label("板端目录", "muted"), self.remote_dir, spacing=8))
        self.progress = QProgressBar()
        self.progress.setObjectName("bitProgress")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(4)   # thin line, like the main-window progress
        # Tween toward the target so the line advances smoothly instead of stepping.
        from PySide6.QtCore import QPropertyAnimation, QEasingCurve
        self._anim = QPropertyAnimation(self.progress, b"value", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        layout.addWidget(self.progress)
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.upload_button = button("上传", self.start_upload, "primary", "write")
        self.upload_button.setMinimumWidth(110)
        layout.addLayout(row(1, self.upload_button))

        # Ctrl+V pastes a copied .bit file (or a raw file path) from the clipboard.
        QShortcut(QKeySequence.Paste, self, self.paste_file)

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 .bit 文件", self._local_dir, "bit file (*.bit);;所有文件 (*)")
        if path:
            self.set_path(path)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择包含 .bit 的文件夹", self._local_dir)
        if folder:
            self.scan_folder(folder)

    def scan_folder(self, folder):
        """Find .bit files under `folder` off the GUI thread; several → let the user pick."""
        if self._scan_worker is not None:
            return
        self.status.setText("正在查找文件夹及子目录中的 .bit 文件…")
        self.choose_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        worker = Worker(lambda progress: file_scan.find_files(folder, (".bit",)))
        self._scan_worker = worker
        worker.signals.result.connect(lambda result: self._scan_done(folder, *result))
        worker.signals.failed.connect(lambda message: self.status.setText("查找 .bit 失败：" + message))
        worker.signals.finished.connect(self._scan_finished)
        QThreadPool.globalInstance().start(worker)

    def _scan_finished(self):
        self._scan_worker = None
        self.choose_button.setEnabled(True)
        self.folder_button.setEnabled(True)

    def _scan_done(self, folder, entries, complete):
        self._local_dir = str(folder)
        self.local_dir_changed.emit(self._local_dir)
        partial = "" if complete else "（目录过深或文件过多，仅列出部分结果）"
        if not entries:
            self.status.setText(f"文件夹内未找到 .bit 文件{partial}：{folder}")
            return
        if len(entries) == 1:
            self.set_path(str(entries[0][0]))
            return
        rows = [(path, [file_time(mtime), f"{size:,} 字节"]) for path, mtime, size in entries]
        chooser = FileChoiceDialog("选择要上传的 bit", f"在 {folder} 下找到 {len(entries)} 个 .bit 文件{partial}，"
                                   "按修改时间从新到旧排列，双击或点「选择」确定。", folder, rows,
                                   ["修改时间", "大小"], self)
        if chooser.exec() == QDialog.Accepted and chooser.selected_path():
            self.set_path(chooser.selected_path())
        else:
            self.status.setText(f"找到 {len(entries)} 个 .bit 文件，未选择。")

    def set_percent(self, percent):
        """Slide the line smoothly to the target percentage."""
        self._anim.stop()
        self._anim.setStartValue(self.progress.value())
        self._anim.setEndValue(max(self.progress.value(), percent))
        self._anim.setDuration(220)
        self._anim.start()

    def set_done(self):
        self._anim.stop()
        self.progress.setValue(100)

    def set_reset(self):
        self._anim.stop()
        self.progress.setValue(0)

    def set_path(self, path):
        """Accept a selected/dropped/pasted bit path. Strictly requires the .bit suffix."""
        if not str(path).lower().endswith(".bit"):
            QMessageBox.warning(self, "文件类型错误", "选中文件不是bit文件，请选择 .bit 文件。")
            return
        local = Path(path)
        try:
            size = local.stat().st_size
        except OSError:
            # A stale clipboard file URL or a vanished network path must not
            # raise out of the Qt slot: reject with a message instead.
            QMessageBox.warning(self, "文件不存在", f"找不到文件：{path}\n该路径可能已失效或不可访问。")
            return
        self._local_path = path
        self.path_label.setText(path)
        self.path_label.setToolTip(path)
        self.set_reset()
        self.status.setText(f"本地文件：{local.name}（{size:,} 字节）")
        if str(local.parent) != self._local_dir:
            self._local_dir = str(local.parent)
            self.local_dir_changed.emit(self._local_dir)

    def paste_file(self):
        """Accept a .bit file pasted from the clipboard (Ctrl+V)."""
        local = self._clipboard_bit_path()
        if local:
            self.set_path(local)
        else:
            self.status.setText("剪贴板中没有 .bit 文件路径：请在文件资源管理器中复制一个 .bit 文件再粘贴。")

    def _clipboard_bit_path(self):
        """Resolve a .bit file from the clipboard, or None. Handles copied files
        (URLs / file:// URIs) and raw Windows/Unix path text."""
        mime = QGuiApplication.clipboard().mimeData()
        if mime.hasUrls():
            for url in mime.urls():
                local = url.toLocalFile()
                if local.lower().endswith(".bit"):
                    return local
        for raw in (mime.text().strip().strip('"'),) if mime.hasText() else ():
            candidate = QUrl(raw).toLocalFile() if raw.startswith("file://") else raw
            candidate = candidate.strip('"')
            if candidate.lower().endswith(".bit"):
                try:
                    if Path(candidate).is_file():
                        return candidate
                except OSError:
                    pass
        return None

    def closeEvent(self, event):
        self._reset_state()
        event.accept()

    def reject(self):
        self._reset_state()
        super().reject()

    def _reset_state(self):
        """Fresh dialog each open: require a new pick/drag/paste."""
        self._local_path = None
        self.path_label.setText("未选择文件")
        self.path_label.setToolTip("")
        self.set_reset()
        self.status.clear()

    @staticmethod
    def _droppable(url):
        local = url.toLocalFile()
        return bool(local) and (local.lower().endswith(".bit") or Path(local).is_dir())

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(self._droppable(u) for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if self._droppable(url):
                local = url.toLocalFile()
                if Path(local).is_dir():
                    self.scan_folder(local)
                else:
                    self.set_path(local)
                break

    def start_upload(self):
        if not self._local_path:
            return
        if not str(self._local_path).lower().endswith(".bit"):
            QMessageBox.warning(self, "文件类型错误", "选中文件不是bit文件，请选择 .bit 文件。")
            return
        self.upload_requested.emit(self._local_path, self.remote_dir.path_text())


class BitRollbackDialog(QDialog):
    """Pick a timestamped bit backup to restore as the live sunny_fpga.bit.

    The window lists board-side backups, then runs the rollback rename on a worker
    thread; this dialog stays a stateless view (mirroring BitUploadDialog)."""
    rollback_requested = Signal(str)  # backup file name
    remote_dir_changed = Signal(str)

    def __init__(self, parent=None, remote_dirs=(), remote_dir=DEFAULT_BIT_DIR):
        super().__init__(parent)
        self.setWindowTitle("回退bit")
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self.setWindowModality(Qt.NonModal)
        self.setMinimumSize(560, 420)
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(label("选择要回退的 bit 版本", "title"))
        hint = label("列出所选板端目录下的 sunny_fpga.bit* 备份。回退时当前 "
                     "sunny_fpga.bit 会先备份为 sunny_fpga.bit_时间戳，再把所选版本恢复为 sunny_fpga.bit。", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.remote_dir = RemoteDirCombo(remote_dirs, remote_dir)
        self.remote_dir.committed.connect(self._directory_committed)
        layout.addLayout(row(label("板端目录", "muted"), self.remote_dir, spacing=8))
        self._listed_dir = None
        self.list = QListWidget()
        self.list.setObjectName("rollbackList")
        self.list.setAlternatingRowColors(True)
        self.list.itemDoubleClicked.connect(self.start_rollback)
        layout.addWidget(self.list, 1)
        self.progress = QProgressBar()
        self.progress.setObjectName("bitProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        layout.addWidget(self.progress)
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.rollback_button = button("回退到所选版本", self.start_rollback, "primary")
        self.rollback_button.setMinimumWidth(140)
        self.rollback_button.setEnabled(False)
        self.cancel_button = button("关闭", self.reject, "flat")
        self.cancel_button.setAutoDefault(False)
        layout.addLayout(row(1, self.cancel_button, self.rollback_button))
        self.list.currentRowChanged.connect(
            lambda _row: self.rollback_button.setEnabled(not self._busy and self.list.currentItem() is not None))

    def _directory_committed(self, directory):
        if directory != self._listed_dir and not self._busy:
            self.remote_dir_changed.emit(directory)

    def set_listed_dir(self, directory):
        self._listed_dir = directory

    def listed_dir(self):
        return self._listed_dir

    def set_busy(self, busy):
        self._busy = busy
        self.list.setEnabled(not busy)
        self.remote_dir.setEnabled(not busy)
        self.rollback_button.setEnabled(not busy and self.list.currentItem() is not None)

    def populate(self, entries, status=""):
        self.list.clear()
        for entry in entries:
            name = entry["name"]
            stamp = entry.get("timestamp")
            if stamp:
                human = stamp
                if re.fullmatch(r"\d{14}", stamp):
                    try:
                        human = datetime.strptime(stamp, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        pass   # not a real calendar date: show the raw board-side stamp
                text = f"{name}    备份于 {human}"
            else:
                text = f"{name}    当前版本"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, name)   # the actual backup file name
            if not stamp:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)   # live bit is not a rollback target
            self.list.addItem(item)
        self.status.setText(status)
        if entries:
            self.list.setCurrentRow(self._find_rollback_target())
        self.set_busy(False)

    def _find_rollback_target(self):
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.flags() & Qt.ItemIsEnabled:
                return row
        return -1

    def selected_backup(self):
        item = self.list.currentItem()
        if item:
            return item.data(Qt.UserRole)
        return None

    def start_rollback(self):
        backup = self.selected_backup()
        if not backup or self._busy:
            return
        self.rollback_requested.emit(backup)

    def set_percent(self, percent):
        self.progress.setValue(max(self.progress.value(), percent))

    def set_done(self):
        self.progress.setValue(100)

    def set_reset(self):
        self.progress.setValue(0)


def show_help(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("使用指南")
    dialog.resize(720, 520)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 24, 24, 20)
    layout.addWidget(label("使用指南", "title"))
    browser = QTextBrowser()
    browser.setHtml("""
    <style>
      body { color:#344B60; font-family:'Microsoft YaHei UI'; font-size:12px; }
      h3 { color:#2463DC; margin:18px 0 6px; padding-left:9px; border-left:3px solid #2463DC; font-size:15px; font-weight:normal; }
      p { font-family:'Microsoft YaHei UI'; font-size:12px; line-height:1.7; margin:0 0 4px; }
      code { font-family:'Microsoft YaHei UI'; color:#C4622D; font-size:12px; }
      kbd { font-family:Consolas; color:#344B60; background:#EDF1F5; border:1px solid #D4DCE5; border-bottom-width:2px; padding:0 5px; border-radius:3px; font-size:11px; }
    </style>
    <h3>1. 连接设备</h3><p>左栏「设备连接」卡片：SSH 行填写主板地址、端口、用户名和密码后点 <code>连接</code>；连接中按钮变红为 <code>取消</code>，可随时中止。serial 行选本机端口与波特率后点 <code>连接</code>，终端自动切到 <code>serial</code> 实时显示板卡输出。SSH 与 serial 相互独立、可同时在线，状态见各行圆点；寄存器读写始终走 SSH。串口线拔出会立即断开并提示。</p>
    <h3>2. 导入 top 并选择组件</h3><p>连接前先点 <code>导入 top</code>：可直接选 emcc mix 顶层文件，或选文件夹自动查找其下（含子目录）包含 ec_ 控件的 top，找到多个时列表选择。组件按类型分组列出，点击组件即加载其精确寄存器表并自动读取。基地址自动取自 <code>components_param.vh</code> 。</p>
    <h3>3. 切换视图读写</h3><p>用 <code>基础 / A通道 / B通道 / C通道 / 中断 / 参数 / 调试 / 全部</code> 页签切换视图。选中行后右侧显示位状态与字段解析；双击待写入值或选预设，点 <code>写入并回读</code> 。<code>批量写入</code> 先预览再执行；<code>自动读取</code> 按所选间隔轮询。</p>
    <h3>4. 打包 / 上传 / 回退 bit、日志与重启</h3><p><code>打包bit</code> 打开内置 Bit_pack 本地打包窗口，无需连接设备；选择或拖入 .bit、填写项目号与输出目录后生成 <code>项目号_bit_时间戳.zip</code>，ZIP 内为 <code>sunny_fpga.bit</code>，源文件保留。弹窗与工作台风格一致，重复点击复用窗口；取消或关闭窗口时先安全取消并清理任务，再关闭。<code>上传bit</code> 选择文件、选择文件夹（自动查找其下 .bit，多个时列表选择）或拖入 .bit，上传到下拉选择的板端目录（默认 <code>/run/media/sda</code>，自动列出板端 /run/media 下的挂载目录，记住上次使用的目录），完整传完后才备份板端旧文件并换入为 <code>sunny_fpga.bit</code>（传输失败原文件不变）；<code>回退bit</code> 列出所选板端目录下的时间戳备份，选择版本后当前 bit 先备份、所选版本恢复为 <code>sunny_fpga.bit</code> ；<code>下载log</code> 把板端 <code>sunny.log</code> 保存到本地；<code>重启设备</code> 发送 reboot 并断开。</p>
    <h3>5. 会话终端</h3><p>来源选 <code>ssh</code> 时在底部 <code>❯</code> 提示行输入命令，回车执行，<kbd>↑</kbd><kbd>↓</kbd> 翻历史。来源选 <code>serial</code> 时终端就是串口控制台：板卡自发的打印（启动日志、内核消息）实时显示，按键逐个直发板卡（Tab 补全、<kbd>↑</kbd><kbd>↓</kbd> 历史由板端 shell 处理）；进 U-Boot：板卡启动倒计时时点一下终端并按住 <kbd>Shift</kbd>+<kbd>7</kbd>，或先勾选工具栏 <code>拦截U-Boot</code> 再重启/上电，软件会在倒计时出现时自动按住 &amp; 直到停在 U-Boot 提示符。无选中文字时 <kbd>Ctrl+C</kbd> 发送中断，有选中时复制；<kbd>Ctrl+V</kbd> 粘贴发送。<code>tail log</code> 与 <code>system</code> 视图只读。SSH 连上后自动后台打印板端 <code>sunny.log</code>，切到 <code>tail log</code> 查看；连接生命周期等软件消息在 <code>system</code> 。<code>查找日志</code> 随输随查并显示 当前/总数 计数，选中终端文字后按 <kbd>Ctrl+F</kbd> 自动填充；<code>换行</code> / <code>跟随</code> 控制显示；<code>导出</code> 保存当前终端内容，<code>清空</code> 清除当前终端。</p>
    <h3>6. 组件定义更新</h3><p>RTL 组件内部修改后，点 <code>导入组件</code> 选择该组件文件夹（或上级目录批量导入），重新解析寄存器定义，立即生效并保存到本机。</p>
    <h3>7. 快捷键</h3><p><kbd>F5</kbd> 读取全部 · <kbd>Ctrl+F</kbd> 查找日志 · <kbd>Ctrl+L</kbd> 终端聚焦 · <kbd>Ctrl+Shift+S</kbd> 导出快照 · <kbd>Esc</kbd> 停止后续操作。</p>
    <h3>8. 开发者</h3><p>szzhang / cgliu / bxli</p>
    """)
    layout.addWidget(browser, 1)
    layout.addLayout(row(1, button("知道了", dialog.accept, "primary")))
    dialog.exec()
