# -*- coding: utf-8 -*-
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
                               QHeaderView, QAbstractItemView, QDialogButtonBox, QPlainTextEdit,
                               QFileDialog, QMessageBox, QTextBrowser, QLineEdit, QCheckBox, QSpinBox,
                               QPushButton, QProgressBar)
from .core import write_command
from . import __version__
from .widgets import label, button, row
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


class BitUploadDialog(QDialog):
    """Pick or drop a .bit file and send it to the board as sunny_fpga.bit."""
    upload_requested = Signal(str, str)  # local path, remote dir

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("上传bit file")
        self.setWindowFlags(self.windowFlags() | Qt.Window)
        self.setWindowModality(Qt.NonModal)
        self.setMinimumSize(540, 240)
        self.setAcceptDrops(True)
        self._local_path = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(label("选择或拖入 .bit 文件", "title"))
        hint = label("上传后板端原 sunny_fpga.bit 会重命名为 sunny_fpga.bit_时间戳 备份，新文件以 sunny_fpga.bit 落到 /run/media/sda。", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.path_label = label("未选择文件", "mono")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)
        choose = button("选择文件…", self.choose_file, "flat", "export")
        self.remote_dir = QLineEdit("/run/media/sda")
        self.remote_dir.setObjectName("mono")
        layout.addLayout(row(label("板端目录", "muted"), self.remote_dir, 1, choose, spacing=8))
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

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 .bit 文件", "", "bit file (*.bit);;所有文件 (*)")
        if path:
            self.set_path(path)

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
        self._local_path = path
        self.path_label.setText(path)
        self.path_label.setToolTip(path)
        self.set_reset()
        self.status.setText(f"本地文件：{Path(path).name}（{Path(path).stat().st_size:,} 字节）")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith(".bit") for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local.lower().endswith(".bit"):
                self.set_path(local)
                break

    def start_upload(self):
        if self._local_path:
            self.upload_requested.emit(self._local_path, self.remote_dir.text().strip() or "/run/media/sda")


def show_help(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("使用指南")
    dialog.resize(720, 610)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 24, 24, 20)
    layout.addWidget(label("从连接到寄存器调试", "title"))
    browser = QTextBrowser()
    browser.setHtml("""
    <style>body {color:#344B60;font-family:'Microsoft YaHei UI';font-size:13px} h3{color:#2463DC;margin-top:20px} p{line-height:1.65} code{font-family:Consolas;color:#2463DC}</style>
    <h3>1. 连接设备</h3><p>填写主机、端口、用户名和密码，点击「连接设备」。设备需要提供 SSH shell 和 devmem 命令。</p>
    <p>重启板卡后若出现主机密钥变化提示，窗口会显示上次记录与本次连接的 SHA-256 指纹。核对设备地址与新指纹后，点击「信任新密钥并重连」。软件仅更新该设备的记录，并自动备份原记录；取消会保留原记录并停止连接。</p>
    <h3>2. 导入 top 并选择组件</h3><p>点击侧栏「导入 top」选择 emcc mix 顶层文件：组件目录按类型分组列出全部设备（可按设备名 / 类型 / 地址搜索，禁用组件灰色且不可选）。点击组件即加载该类型的精确寄存器表（寄存器名与 RTL 宏一致），标题行显示设备名、类型徽标和全地址；基地址自动取自 components_param.vh，找不到时左侧出现可编辑的基地址框。重启后自动重新加载上次导入的 top。</p>
    <p>用「全部 / 基础 / A通道 / B通道 / C通道 / 中断 / 参数 / 调试」页签切换视图：<b>基础</b>是身份与状态头（RST_EN 写 1 运行 / 0 复位保持、BHV_PRIORITY 仲裁序、M_WK_MOD=3 手动模式）；<b>A/B/C通道</b>按通道分列各自的 EN 门控、BHV_ID 触发（预设来自 RTL 行为表）、TX_OT 秒级超时、TX_ID 事务号、ALM_NUM 报警码（A 含任务标记，C 含心跳周期）；<b>中断</b>含 IRQ 字段解码与 A/B/C 上报应答（TX_RSULT_RPT 写 0x51/0x52 应答）；<b>参数</b>的寄存器名带实际信号名（如 PARAM1 · rcfg_spd_max），悬停另显示 RTL 注释；<b>调试</b>的 DEBUG_REG 按字节解码状态机历史（前第3拍…当前）。读取全部、轮询和批量写入只作用于当前视图。无精确表的类型带 ≈ 标记并使用通用回退。</p>
    <p>某组件 RTL 内部修改后，点「导入组件」选择该组件文件夹即可重新解析其寄存器定义（无需改源码或重新打包）：解析结果立即生效并保存到本机 %LOCALAPPDATA%/DevmemStudio/component_overrides，重启自动加载，删除对应文件即恢复内置定义。</p>
    <h3>3. 读取、检查与写入</h3><p>点击行内读取按钮，或使用「读取全部」。选中行后，右侧同步显示 HEX、DEC、32 位状态及 irq / rpt 字段解析。待写入值可在表格中双击编辑，也可在检查器中使用 HEX / DEC 与预设。点击「写入并回读」才会写入设备。</p>
    <p>「解析与位状态」逐行展示字段名、位区间和数值。irq1 按 ec_id [31:18]、sc_id [17:8]、r_a_bhv_id [7:0] 解析；irq2 按 r_a_tx_id [31:24]、r_a_alm_num [23:16] 解析，均为十进制。a / b / c rpt 从高到低依次为 ack_beh_id、ack_tx_id、ack_tx_result、ack_ps_alart_num，各占 8 位；仅 ack_tx_result 使用十六进制，其余使用十进制。悬停当前值及导出 CSV 使用相同字段名和进制。</p>
    <p>字段长度 1 / 20 位默认使用 32 位总线访问。读取命令沿用原程序：<code>devmem 0xADDR</code>。写入命令：<code>devmem 0xADDR WIDTH 0xVALUE</code>。访问位宽与目标板的 devmem 支持保持一致。回读值为实测值，动作寄存器可能自动清零。</p>
    <h3>4. 轮询与批量操作</h3><p>「自动读取」按设定间隔轮询当前模块；上一次任务完成后才执行下一次，不堆积请求。「批量写入」先列出准确地址和数值，再勾选执行。动作和伺服按钮需要主动勾选。任务遇错停止，停止按钮在当前命令完成后生效。</p>
    <h3>5. 命令与记录</h3><p>下方支持 shell 命令；读 HEX / DEC 把输入地址转换为 devmem 读取。↑ / ↓ 浏览命令历史。点击「打印日志」会立即打开独立窗口并执行 <code>tail -f /run/media/sda/sunny.log</code>，无需再次点击开始。日志启动和打印期间，主窗口均可继续读写寄存器；停止或关闭日志窗口只结束日志通道。可导出 CSV 快照、会话日志与板端日志。</p>
    <p>日志窗口默认开启「自动换行」和「显示行号」，可分别勾选切换。长日志按窗口宽度折行，续行不重复编号，复制和保存仍保留原文。左侧行号对应当前缓存中的原始行，Ctrl+G 输入同一行号并按 Enter 跳转。最多保留最近 5000 行，旧行淘汰或清空后按当前缓存重新编号。</p>
    <p>日志按错误、告警、成功、调试分级高亮。Ctrl+F 查找普通文本，可选择区分大小写；Enter / F3 跳到下一处，Shift+Enter / Shift+F3 跳到上一处，首尾循环。查找框中 Esc 清空查找。查找和向上浏览时暂停自动滚动，日志继续接收；「回到最新」恢复自动跟随。保存日志导出当前缓存的纯文本。</p>
    <h3>快捷键</h3><p>F5 读取全部 · Ctrl+F 搜索寄存器 · Ctrl+L 聚焦命令 · Ctrl+Shift+S 导出快照 · Esc 停止后续批量操作。</p>
    <h3>配置与版本</h3><p>寄存器调试工作台，版本 {version}，作者 szzhang / cgliu / bxli。配置优先存放于程序旁 registers.json；目录不可写时使用 %LOCALAPPDATA%/DevmemStudio。勾选「记住密码」将密码保存在本机配置中。寄存器模板在 devmem_studio/catalog.py。</p>
    """.replace("{version}", __version__))
    layout.addWidget(browser, 1)
    layout.addLayout(row(1, button("知道了", dialog.accept, "primary")))
    dialog.exec()
