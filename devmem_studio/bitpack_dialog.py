"""Workbench-styled local Bit_pack dialog; the native engine stays headless."""
from pathlib import Path
import re

from PySide6.QtCore import Qt, Signal, QThreadPool, QUrl, QTimer
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFrame, QLineEdit, QCheckBox,
                               QSpinBox, QProgressBar, QPlainTextEdit, QFileDialog, QMessageBox, QComboBox, QSizePolicy)

from . import bitpack
from .core import application_dir
from .widgets import label, button, row, field, Worker, restyle, ComboBox, ElidedLabel


class BitPackDialog(QDialog):
    job_finished = Signal()
    settings_changed = Signal(object)

    def __init__(self, parent=None, directory=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle("FPGA Bit 打包")
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.setAcceptDrops(True)
        self.resize(820, 660)
        self.setMinimumSize(640, 540)
        self.busy = False
        self._worker = None
        self._cancel = None
        self._close_pending = False
        self._result_path = None
        self._preferences = bitpack.normalize_preferences(settings)
        self._output_custom = bool(self._preferences["output_directory"])
        self._source_mode = self._preferences["source_mode"]
        self._source_directory = self._preferences["source_directory"]
        self._preferred_source = self._preferences["source_path"]
        self._updating_source = False
        self._source_dirty = False
        self._job_kind = "pack"
        self._restore_folder_pending = self._source_mode == "folder" and bool(self._source_directory)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        initial = Path(directory) if directory is not None else application_dir()
        if not initial.is_dir():
            initial = application_dir()
        if not self._source_directory:
            self._source_directory = str(initial)
        remembered = Path(self._source_directory)
        self._browse_directory = str(remembered if remembered.is_dir() else initial)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        layout.addLayout(row(label("FPGA Bit 打包", "title"), 1, label("本地工具 · Bit_pack 2.0.1", "badge")))
        note = label("选择 .bit 文件，生成项目发布包。源文件始终保留，ZIP 发布前自动校验。", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        card = QFrame()
        card.setObjectName("card")
        fields = QVBoxLayout(card)
        fields.setContentsMargins(18, 16, 18, 18)
        fields.setSpacing(12)
        fields.addWidget(label("打包设置", "sectionTitle"))
        self.project = QLineEdit(self._preferences["project"])
        self.project.setPlaceholderText("例如 A100（1–63 个字符）")
        self.project.setMaxLength(63)
        self.project.setAccessibleName("项目号")
        fields.addWidget(field("项目号", self.project))
        self.source_combo = ComboBox()
        self.source_combo.setEditable(True)
        self.source_combo.setInsertPolicy(QComboBox.NoInsert)
        self.source_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.setMinimumContentsLength(12)
        self.source_combo.setMaxVisibleItems(15)
        self.source_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.source = self.source_combo.lineEdit()
        self.source.setPlaceholderText("选择文件，或选择文件夹扫描 .bit")
        self.source.setAccessibleName("源 bit 文件")
        self.source.textChanged.connect(self.source.setToolTip)
        self.source.textChanged.connect(self.source_combo.setToolTip)
        self.choose_source = button("选择文件", self.browse_source, None, "import")
        self.choose_folder = button("选择文件夹", self.browse_folder, None, "folder")
        fields.addWidget(label("源文件 / 文件夹", "muted"))
        fields.addLayout(row(self.source_combo, self.choose_source, self.choose_folder))
        self.directory_caption = ElidedLabel("", "muted")
        self.directory_caption.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        fields.addWidget(self.directory_caption)
        if self._preferred_source:
            self.source_combo.addItem(self._preferred_source)
        self.output = QLineEdit(self._preferences["output_directory"] or str(initial))
        self.output.setAccessibleName("输出目录")
        self.output.setToolTip(self.output.text())
        self.output.textChanged.connect(self.output.setToolTip)
        self.output.textEdited.connect(self._output_edited)
        self.choose_output = button("选择目录", self.browse_output, None, "folder")
        fields.addWidget(label("输出目录", "muted"))
        fields.addLayout(row(self.output, self.choose_output))
        self.nopack = QCheckBox("仅复制为 sunny_fpga.bit，不压缩")
        self.nopack.toggled.connect(lambda on: self.project.setEnabled(not on and not self.busy))
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 86400)
        self.timeout.setValue(300)
        self.timeout.setSuffix(" 秒")
        self.timeout.setFixedWidth(112)
        self.timeout.setAccessibleName("任务超时秒数")
        fields.addLayout(row(self.nopack, 1, label("超时", "muted"), self.timeout))
        layout.addWidget(card)

        self.status = label("就绪 · 无需连接设备", "badge")
        self.status.setProperty("state", "offline")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setObjectName("bitProgress")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedHeight(4)
        layout.addWidget(self.progress)
        self.details = QPlainTextEdit()
        self.details.setObjectName("console")
        self.details.setReadOnly(True)
        self.details.setMaximumBlockCount(300)
        self.details.setPlaceholderText("任务阶段、校验结果和输出路径会显示在这里。")
        layout.addWidget(self.details, 1)
        self.open_output_button = button("打开输出目录", self.open_output, "flat", "folder")
        self.open_output_button.setEnabled(False)
        self.cancel_button = button("取消任务", self.cancel_task, "danger", "stop")
        self.cancel_button.setVisible(False)
        self.close_button = button("关闭", self.close)
        self.start_button = button("开始打包", self.start_pack, "primary", "package")
        self.start_button.setMinimumWidth(120)
        self.start_button.setDefault(True)
        layout.addLayout(row(self.open_output_button, 1, self.cancel_button, self.close_button, self.start_button))
        QShortcut(QKeySequence("Alt+P"), self, self.project.setFocus)
        QShortcut(QKeySequence("Alt+I"), self, self.source.setFocus)
        QShortcut(QKeySequence("Alt+O"), self, self.output.setFocus)
        self.project.textChanged.connect(lambda _: self._remember_state())
        self.output.textChanged.connect(lambda _: self._remember_state())
        self.source_combo.currentIndexChanged.connect(self._source_selected)
        self.source.textEdited.connect(self._source_edited)
        self.source.editingFinished.connect(self._commit_source_edit)
        self._update_directory_caption()

    def _output_edited(self, _):
        self._output_custom = True
        self._remember_state()

    def preference_snapshot(self):
        return {"project": self.project.text(), "source_directory": self._source_directory,
                "output_directory": self.output.text(), "source_path": self.source.text(),
                "source_mode": self._source_mode}

    def _remember_state(self):
        if not self._updating_source:
            self.settings_changed.emit(self.preference_snapshot())

    def _update_directory_caption(self):
        prefix = "文件夹：" if self._source_mode == "folder" else "选择目录："
        self.directory_caption.setText(prefix + self._source_directory)

    def _source_selected(self, index):
        if self._updating_source or self.busy or index < 0:
            return
        selected = Path(self.source.text())
        if selected.is_file() and not self._output_custom:
            self.output.setText(str(selected.parent))
        self._remember_state()

    def _source_edited(self, _):
        self._source_dirty = True

    def _commit_source_edit(self):
        if self._source_dirty and not self.busy:
            selected = Path(self.source.text().strip().strip('"'))
            if selected.is_file() and selected.suffix.lower() == ".bit":
                self._source_mode = "file"
                self._source_directory = str(selected.parent)
                self._browse_directory = self._source_directory
                if not self._output_custom:
                    self.output.setText(str(selected.parent))
                self._update_directory_caption()
            self._source_dirty = False
            self._remember_state()

    def showEvent(self, event):
        super().showEvent(event)
        if self._restore_folder_pending:
            self._restore_folder_pending = False
            root = Path(self._source_directory)
            if root.is_dir():
                QTimer.singleShot(0, lambda: self.scan_directory(root))
            else:
                self._set_status("上次选择的文件夹暂时无法访问，请重新选择。", "offline")

    def browse_folder(self):
        path = QFileDialog.getExistingDirectory(self, "选择包含 .bit 的文件夹", self._browse_directory)
        if path:
            self.scan_directory(path)

    def scan_directory(self, directory):
        if self.busy:
            return
        raw = str(directory).strip().strip('"')
        root = Path(raw)
        if not raw or not root.is_dir():
            QMessageBox.warning(self, "目录无效", "请选择可以访问的文件夹。")
            return
        try:
            bitpack.prepare_bitpack_executable()
            self._cancel = bitpack.CancelEvent()
        except OSError as exc:
            QMessageBox.warning(self, "无法扫描目录", str(exc))
            return
        self._preferred_source = self.source.text()
        self._source_mode = "folder"
        self._source_directory = str(root)
        self._browse_directory = str(root)
        self._job_kind = "scan"
        self._result_path = None
        self._close_pending = False
        self.open_output_button.setEnabled(False)
        self.details.clear()
        self._update_directory_caption()
        self._set_status("正在扫描文件夹及子目录中的 .bit 文件…", "connecting")
        self._set_busy(True)
        self._remember_state()
        worker = Worker(lambda progress: bitpack.scan_bitfiles(root, self._cancel, progress))
        self._worker = worker
        worker.signals.progress.connect(self._on_progress)
        worker.signals.result.connect(self._on_scan_result)
        worker.signals.failed.connect(self._on_failure)
        worker.signals.finished.connect(self._on_finished)
        self.pool.start(worker)

    def _on_scan_result(self, result):
        if result["code"] != 0:
            self._set_status("目录扫描已取消" if result["code"] == 3 else "扫描失败 · 请查看任务日志",
                             "offline" if result["code"] == 3 else "error")
            return
        files = result["files"]
        self._updating_source = True
        try:
            self.source_combo.clear()
            self.source_combo.addItems(files[:2000])
            if files:
                chosen = self._preferred_source if self._preferred_source in files else files[0]
                index = self.source_combo.findText(chosen)
                if index >= 0:
                    self.source_combo.setCurrentIndex(index)
                else:
                    self.source_combo.setEditText(chosen)
                if not self._output_custom:
                    self.output.setText(str(Path(chosen).parent))
        finally:
            self._updating_source = False
        text = f"找到 {len(files)} 个 .bit 文件，请从下拉列表选择源文件。" if files else "文件夹内未找到 .bit 文件，请重新选择。"
        if len(files) > 2000:
            text += " 列表显示前 2000 项，其它文件可粘贴完整路径。"
        if result["skipped"] or result["truncated"]:
            text += f" 跳过 {result['skipped']} 项（联接、权限或深度限制），请检查扫描完整性。"
        self.details.appendPlainText(text)
        self._set_status(text, "demo" if result["skipped"] or result["truncated"] else "offline")
        self._remember_state()

    def browse_source(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 .bit 文件", self._browse_directory,
                                            "Bit 文件 (*.bit);;所有文件 (*)")
        if path:
            self.set_source(path)

    def set_source(self, path):
        if self.busy:
            return
        source = Path(str(path).strip().strip('"'))
        if source.suffix.lower() != ".bit" or not source.is_file():
            QMessageBox.warning(self, "文件无效", "请选择存在的 .bit 文件。")
            return
        self._updating_source = True
        try:
            self.source_combo.clear()
            self.source_combo.addItem(str(source))
        finally:
            self._updating_source = False
        self._source_mode = "file"
        self._source_directory = str(source.parent)
        self._browse_directory = self._source_directory
        self._update_directory_caption()
        if not self._output_custom:
            self.output.setText(str(source.parent))
        self._set_status("已选择源文件 · 源文件保留", "offline")
        self._remember_state()

    def browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if path:
            self.output.setText(path)
            self._output_custom = True

    def dragEnterEvent(self, event):
        if not self.busy and event.mimeData().hasUrls() and any(
                url.isLocalFile() and (url.toLocalFile().lower().endswith(".bit") or Path(url.toLocalFile()).is_dir())
                for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self.busy:
            return
        for url in event.mimeData().urls():
            if url.isLocalFile() and (url.toLocalFile().lower().endswith(".bit") or Path(url.toLocalFile()).is_dir()):
                if Path(url.toLocalFile()).is_dir():
                    self.scan_directory(url.toLocalFile())
                else:
                    self.set_source(url.toLocalFile())
                event.acceptProposedAction()
                break

    def _set_status(self, text, state):
        self.status.setText(text)
        self.status.setProperty("state", state)
        restyle(self.status)

    def _set_busy(self, busy):
        self.busy = busy
        for widget in (self.source_combo, self.source, self.output, self.choose_source, self.choose_folder, self.choose_output,
                       self.nopack, self.timeout, self.start_button):
            widget.setEnabled(not busy)
        self.project.setEnabled(not busy and not self.nopack.isChecked())
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)
        self.close_button.setText("取消并关闭" if busy else "关闭")
        self.progress.setRange(0, 0 if busy else 100)
        if not busy:
            self.progress.setValue(100 if self._result_path else 0)

    def start_pack(self):
        if self.busy:
            return
        source = Path(self.source.text().strip().strip('"'))
        output = Path(self.output.text().strip().strip('"'))
        self._commit_source_edit()
        self._remember_state()
        project = self.project.text()
        nopack = self.nopack.isChecked()
        if source.suffix.lower() != ".bit" or not source.is_file():
            QMessageBox.warning(self, "文件无效", "请选择存在的 .bit 文件。")
            self.source.setFocus()
            return
        if not self.output.text().strip().strip('"') or not output.is_dir():
            QMessageBox.warning(self, "目录无效", "请选择已经存在的输出目录。")
            self.output.setFocus()
            return
        if not nopack and (not project or len(project) > 63 or re.search(r'[\s\\/:*?"<>|\x00-\x1f]', project)):
            QMessageBox.warning(self, "项目号无效", "项目号为 1–63 个字符，不能含空白或 \\ / : * ? \" < > |。")
            self.project.setFocus()
            return
        force = False
        target = output / "sunny_fpga.bit"
        if nopack and target.exists():
            try:
                same = source.samefile(target)
            except OSError:
                same = False
            if not same:
                reply = QMessageBox.question(self, "确认替换副本",
                    f"输出目录中已存在 sunny_fpga.bit：\n{target}\n\n是否替换？源文件不会被删除。",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if reply != QMessageBox.Yes:
                    return
                force = True
        try:
            bitpack.prepare_bitpack_executable()
            self._cancel = bitpack.CancelEvent()
        except OSError as exc:
            self._set_status("打包引擎无法启动", "error")
            QMessageBox.warning(self, "无法开始打包", str(exc))
            return
        self._job_kind = "pack"
        self._result_path = None
        self._close_pending = False
        self.open_output_button.setEnabled(False)
        self.details.clear()
        self._set_status("正在复制…" if nopack else "正在打包与校验…", "connecting")
        self._set_busy(True)
        seconds = self.timeout.value()
        worker = Worker(lambda progress: bitpack.run_bitpack(source, output, project, nopack,
                                                            force, seconds, self._cancel, progress))
        self._worker = worker
        worker.signals.progress.connect(self._on_progress)
        worker.signals.result.connect(self._on_result)
        worker.signals.failed.connect(self._on_failure)
        worker.signals.finished.connect(self._on_finished)
        self.pool.start(worker)

    def _on_progress(self, line):
        self.details.appendPlainText(str(line))
        if str(line).startswith(("正在", "校验通过", "tar 不可用")):
            self._set_status(str(line), "connecting")

    def _on_result(self, result):
        code = result["code"]
        if code == 0:
            self._result_path = Path(result["path"])
            self._set_status("复制完成 · 源文件保留" if self.nopack.isChecked() else "打包完成 · ZIP 校验通过 · 源文件保留", "connected")
            self.open_output_button.setEnabled(True)
        elif code == 3:
            self._set_status("已取消 · 源文件和已有输出保持不变", "offline")
        elif code == 4:
            self._set_status("任务超时 · 源文件和已有输出保持不变", "error")
        else:
            self._set_status("打包失败 · 请查看任务日志", "error")

    def _on_failure(self, message):
        self.details.appendPlainText(str(message))
        self._set_status("扫描失败 · 请查看任务日志" if self._job_kind == "scan" else "打包失败 · 请查看任务日志", "error")

    def _on_finished(self):
        if self._cancel is not None:
            self._cancel.close()
            self._cancel = None
        self._worker = None
        self._set_busy(False)
        self._remember_state()
        if self._close_pending:
            self._close_pending = False
            super().reject()
        self.job_finished.emit()

    def cancel_task(self):
        if self.busy and self._cancel is not None:
            try:
                self._cancel.cancel()
            except OSError as exc:
                self.details.appendPlainText(str(exc))
                self._set_status("取消请求失败 · 请等待引擎完成或超时", "error")
                return
            self.cancel_button.setEnabled(False)
            self._set_status("正在取消并清理本次任务…", "connecting")

    def open_output(self):
        if self._result_path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result_path.parent)))

    def reject(self):
        self._remember_state()
        if self.busy:
            self._close_pending = True
            self.cancel_task()
        else:
            super().reject()

    def closeEvent(self, event):
        self._remember_state()
        if self.busy:
            self._close_pending = True
            self.cancel_task()
            event.ignore()
        else:
            super().closeEvent(event)
