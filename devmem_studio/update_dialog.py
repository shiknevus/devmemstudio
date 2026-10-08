"""GitHub update UI, with network work outside the board task pool."""
import threading

from PySide6.QtCore import Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFrame, QCheckBox, QTextBrowser,
                               QProgressBar, QMessageBox, QWidget)

from . import __version__, updater
from .widgets import label, button, row, restyle, Worker


def _megabytes(value):
    return f"{value / 1024 / 1024:.1f} MB"


class UpdateDialog(QDialog):
    job_finished = Signal()
    install_requested = Signal(object)
    preferences_changed = Signal(bool)

    def __init__(self, parent=None, auto_check=True):
        super().__init__(parent)
        self.setWindowTitle("DevmemStudio 更新")
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.resize(640, 560)
        self.setMinimumSize(520, 460)
        self.busy = False
        self.release = None
        self.download = None
        self._checked_once = False
        self._worker = None
        self._cancel = threading.Event()
        self._close_pending = False
        self._automatic = False
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        layout.addLayout(row(label("软件更新", "title"), 1, label("GitHub Releases", "badge")))
        self.status = label("从 GitHub 获取正式发布版本。", "updateStatus")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)

        summary = QFrame()
        summary.setObjectName("card")
        facts = QHBoxLayout(summary)
        facts.setContentsMargins(18, 14, 18, 14)
        facts.setSpacing(0)
        self.current_value = self._fact(facts, "当前版本", __version__)
        self.latest_value = self._fact(facts, "最新版本", "—")
        self.package_value = self._fact(facts, "更新包", "—")
        layout.addWidget(summary)

        notes_card = QFrame()
        notes_card.setObjectName("card")
        notes = QVBoxLayout(notes_card)
        notes.setContentsMargins(18, 14, 10, 12)
        notes.setSpacing(8)
        notes.addWidget(label("更新说明", "sectionTitle"))
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.document().setDocumentMargin(0)
        self.notes.setPlaceholderText("检查到新版本后在此显示更新说明。")
        notes.addWidget(self.notes, 1)
        layout.addWidget(notes_card, 1)

        self.progress_panel = QWidget()
        progress = QVBoxLayout(self.progress_panel)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(6)
        self.progress_text = label("", "muted")
        self.progress_percent = label("", "muted")
        progress.addLayout(row(self.progress_text, 1, self.progress_percent))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        progress.addWidget(self.progress)
        self.progress_panel.hide()
        layout.addWidget(self.progress_panel)

        self.auto_check = QCheckBox("启动时检查更新")
        self.auto_check.setChecked(auto_check)
        self.auto_check.toggled.connect(self.preferences_changed.emit)
        self.release_button = button("打开发布页", self.open_release_page)
        self.cancel_button = button("取消", self.cancel)
        self.cancel_button.hide()
        self.action_button = button("检查更新", self.take_action, "primary")
        self.action_button.setDefault(True)
        layout.addLayout(row(self.auto_check, 1, self.release_button, self.cancel_button, self.action_button))
        self._refresh()

    def _fact(self, layout, title, value):
        block = QVBoxLayout()
        block.setSpacing(4)
        block.addWidget(label(title, "eyebrow"))
        item = label(value, "sectionTitle")
        item.setTextInteractionFlags(Qt.TextSelectableByMouse)
        block.addWidget(item)
        layout.addLayout(block, 1)
        return item

    def _set_status(self, text, state=""):
        self.status.setText(text)
        self.status.setProperty("state", state)
        restyle(self.status)

    def _refresh(self):
        """Footer and summary follow one state: idle / found / downloaded / busy."""
        release = self.release
        self.latest_value.setText(release.version if release else (__version__ if self._checked_once else "—"))
        if release is None:
            self.package_value.setText("—")
        else:
            self.package_value.setText(_megabytes(release.size) if release.size else "大小待下载时确定")
        if self.busy:
            text = "正在下载…" if self.progress_panel.isVisibleTo(self) else "正在检查…"
        elif self.download is not None:
            text = "重启并升级"
        elif release is not None:
            text = "下载更新"
        else:
            text = "重新检查" if self._checked_once else "检查更新"
        self.action_button.setText(text)
        self.action_button.setEnabled(not self.busy)
        self.cancel_button.setVisible(self.busy)
        self.cancel_button.setEnabled(self.busy and not self._cancel.is_set())

    def _start(self, function, callback, downloading=False):
        self.busy = True
        self._cancel.clear()
        self.progress.setRange(0, 0)
        self.progress_text.setText("正在连接 GitHub…")
        self.progress_percent.clear()
        self.progress_panel.setVisible(downloading)
        self._refresh()
        worker = Worker(function)
        self._worker = worker
        worker.signals.result.connect(callback)
        worker.signals.failed.connect(self._failed)
        worker.signals.progress.connect(self._progress)
        worker.signals.finished.connect(self._finished)
        self.pool.start(worker)

    def check(self, automatic=False):
        if self.busy:
            return
        self._automatic = automatic
        self._set_status("正在检查 GitHub 正式发布版本…")
        self._start(lambda progress: updater.check_release(self._cancel), self._checked)

    def _checked(self, release):
        if self._close_pending:
            return
        self._checked_once = True
        self.release = release
        if release is None:
            self._set_status(f"当前 {__version__} 已是最新正式版。", "ok")
            self.notes.clear()
            self.download = None
            return
        if self.download and self.download.version != release.version:
            self.download = None
        self._set_status(f"发现新版本 {release.version}，可下载后重启升级。", "ok")
        self.notes.setMarkdown(release.notes)
        if self._automatic:
            self.show()

    def take_action(self):
        if self.busy:
            return
        if self.download is not None:
            try:
                updater.launcher_path()
            except ValueError as exc:
                QMessageBox.information(self, "重启升级", str(exc))
                return
            answer = QMessageBox.question(self, "重启升级", "将停止当前采样、断开设备并重启升级。旧程序会保留备份。是否继续？")
            if answer == QMessageBox.Yes:
                self.install_requested.emit(self.download)
            return
        if self.release is None:
            self.check()
            return
        self._automatic = False
        self._set_status(f"正在下载 {self.release.version}…")
        self._start(lambda progress: updater.download_release(self.release, self._cancel, progress),
                    self._downloaded, downloading=True)

    def _downloaded(self, download):
        self.download = download
        self._set_status(f"{download.version} 已下载并通过 SHA-256 校验，点击“重启并升级”安装。", "ok")

    def _progress(self, value):
        received, total = value
        if total:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(received * 100 / total))
            self.progress_percent.setText(f"{self.progress.value()}%")
            self.progress_text.setText(f"已下载 {_megabytes(received)} / {_megabytes(total)}")
        else:
            self.progress_text.setText(f"已下载 {_megabytes(received)}")

    def _failed(self, message):
        self._set_status(message, "error")
        if self._automatic and self.parent() is not None:
            self.parent().append_log("SYSTEM", f"检查更新：{message}")

    def _finished(self):
        self.busy = False
        self._worker = None
        self.progress.setRange(0, 100)
        if self.download:
            self.progress.setValue(100)
            self.progress_percent.setText("100%")
            self.progress_text.setText(f"已下载并校验 {self.download.executable.name}")
        else:
            self.progress_panel.hide()
        self._refresh()
        self.job_finished.emit()
        if self._close_pending:
            self.close()

    def open_release_page(self):
        url = updater.RELEASES_URL
        if self.release is not None:
            url += f"/tag/v{self.release.version}"
        QDesktopServices.openUrl(QUrl(url))

    def cancel(self):
        self._cancel.set()
        self.cancel_button.setEnabled(False)
        self._set_status("正在取消，请等待当前网络请求结束…")

    def closeEvent(self, event):
        if self.busy:
            self._close_pending = True
            self.cancel()
            event.ignore()
            return
        self._close_pending = False
        event.accept()

    def reject(self):
        self.close()
