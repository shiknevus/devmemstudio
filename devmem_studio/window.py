# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import csv
from datetime import datetime
import io
import json
from pathlib import Path
import re
import threading
import time

from PySide6.QtCore import Qt, QTimer, QThreadPool, QSize, Slot
from PySide6.QtGui import QIcon, QFont, QColor, QShortcut, QKeySequence, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QLineEdit, QSpinBox, QCheckBox, QComboBox, QLabel, QButtonGroup, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView, QSplitter, QScrollArea,
                               QPlainTextEdit, QProgressBar, QMessageBox, QFileDialog, QApplication,
                               QDialog, QSizePolicy, QLayout, QTreeWidget, QTreeWidgetItem)

from . import __version__
from . import component_parse, top_import
from .catalog import REGISTER_FIELDS
from .core import (ConfigStore, SshSession, DemoSession, ReadbackError, HostKeyChangedError, CommandError, DEFAULT_LOG_PATH, access_width, parse_addr, parse_int,
                   validated_address, write_value, write_command, format_decoded_fields, register_group,
                   resource_path, user_data_dir, session_logger, BATCH_READ_CHUNK)
from .serial_session import SerialSession, list_serial_ports
from .theme import icon
from .widgets import (label, button, row, field, divider, restyle, ComboBox,
                      BitView, DecodedFieldsView, Worker, LogBridge, password_field, IpAddressField, TerminalView)
from .dialogs import BatchDialog, HostKeyDialog, BitUploadDialog, BitRollbackDialog, show_help

# Fixed connect timeout for both SSH and serial: impatient users hit the red
# cancel button instead of tuning a number.
CONNECT_TIMEOUT = 8

VIEW_TOOLTIPS = {"all": "该组件类型的全部实现寄存器",
                 "basic": "身份、模块状态与安全链（公共寄存器头）",
                 "task_a": "A 通道：EN 门控 / BHV_ID 触发 / 超时 / 事务 / 报警 / 通道忙",
                 "task_b": "B 通道：条件触发的控制行为（暂停/停止/复位等，PS 经参数请求）",
                 "task_c": "C 通道：周期心跳行为与 C_GAP_CRL 周期",
                 "irq": "中断寄存器与 A/B/C 上报应答（PS 写 TX_RSULT_RPT 应答 0x51/0x52）",
                 "param": "该类型实现(PARAM)寄存器，名称含实际信号名，含义见悬停注释",
                 "debug": "行为状态机历史(DEBUG_REG，每字节一个状态)"}


class _SidebarScroll(QScrollArea):
    """Sidebar rail whose width comes from the side splitter; content can never
    widen past the viewport.

    widgetResizable(True) alone lets a single wide child (a non-wrapping
    path label, for example) stretch the content past the sidebar width with
    the horizontal scrollbar off, clipping the rest."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebarScroll")
        self.setMinimumWidth(320)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.widget():
            self.widget().setFixedWidth(self.viewport().width())


class MainWindow(QMainWindow):
    def __init__(self, store=None, persist=True):
        super().__init__()
        self.setWindowTitle("DevmemStudio  Auth: szzhang/cgliu/bxli")
        self.setWindowIcon(QIcon(str(resource_path("assets/logo.svg"))))
        self.resize(1540, 960)
        self.setMinimumSize(1180, 740)
        self.store = store or ConfigStore()
        self.cfg = self.store.load()
        self.persist = persist
        self.demo = False
        self.connected = False
        self._busy = False
        self._closing = False
        self._updating = False
        self._active_worker = None
        self._task_callback = None
        self._task_kind = ""
        self._task_serial = None
        self._pending_host_key_change = None
        self._last_context = None
        self._last_fmt = "D"
        self.top_info = None
        self.type_catalog = top_import.load_type_catalog()
        self.active_component = None
        self._pending_component = None
        self.view = "basic"
        self.rtl_base = None
        self.visible_regs = []
        self._module_start = None
        self.cancel = threading.Event()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.stream_pool = QThreadPool(self)
        self.stream_pool.setMaxThreadCount(1)
        self._stream_workers = {}
        self._stream_cancel = threading.Event()
        self._stream_state = "stopped"
        self.bridge = LogBridge(self)
        self.bridge.message.connect(self.append_log)
        self.bridge.stream.connect(self._stream_output)
        self.bridge.stream_stopped.connect(self._stream_stopped)
        self.bridge.stream_ready.connect(self._stream_ready)
        self.bridge.stream_failed.connect(self._stream_failed)
        self.bridge.stream_worker_finished.connect(self._stream_worker_finished)
        self.session = SshSession(self._log_emit("ssh"))
        self.serial = SerialSession(self._log_emit("com"))
        self.serial_connected = False
        self._serial_device = None
        self._serial_busy = False   # serial connect runs outside the global task gate
        self._serial_worker = None
        self._serial_cancel_requested = False
        self.bit_dialog = None
        self.bit_rollback_dialog = None
        self._stream_epoch = 0
        self._stream_finished_epoch = -1
        # Per-source log buffers: console renders whichever the source selector points at.
        # log_records aliases the SSH buffer (the historically flat list tests read).
        self.log_records = []
        self.log_records_ssh = self.log_records
        self.log_records_com = []
        self.log_records_log = []
        self.log_records_system = []
        self.console_source = "system"
        self._log_search_cursor = 0
        self.read_count = 0
        self.write_count = 0
        self.error_count = 0
        try:
            self.file_logger = session_logger(self.store.path.parent / "logs" if not persist else user_data_dir() / "logs")
        except OSError:
            self.file_logger = None
        self.regs = []
        self.selected = None
        self.remote_buttons = []
        self._build()
        self._load_config_fields()
        self.rebuild_registers()
        self._set_connection(False)
        self.append_log("SYSTEM", "工作台就绪。导入 top 并选择组件后读写寄存器。")
        if self.store.warning:
            self.append_log("ERROR", self.store.warning)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(250)
        self._next_poll = 0
        self.heartbeat = QTimer(self)
        self.heartbeat.timeout.connect(self._check_session)
        self.heartbeat.start(1000)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save_settings)
        self._shortcuts()
        if self.type_catalog:
            imported = sorted(key for key, entry in self.type_catalog["types"].items()
                              if entry.get("imported_from"))
            if imported:
                self.append_log("SYSTEM", f"已加载 {len(imported)} 个导入的组件类型定义：{', '.join(imported)}。")
        saved_top = self.cfg.get("top_path")
        if saved_top:
            if Path(saved_top).is_file():
                self.load_top(Path(saved_top), quiet=True)
            else:
                self.append_log("SYSTEM", f"上次导入的 top 文件不存在：{saved_top}；可重新导入。")

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        main = QWidget()
        main.setObjectName("workspace")
        # Explicit floor replaces the toolbar row's ~1400px layout hint: the area
        # already compresses gracefully below it (window min 1180 - sidebar 320).
        main.setMinimumWidth(840)
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self._build_header())
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 18, 22, 14)
        body_layout.setSpacing(14)
        self.vertical_split = QSplitter(Qt.Vertical)
        self.vertical_split.setHandleWidth(9)
        self.vertical_split.setChildrenCollapsible(False)
        self.vertical_split.addWidget(self._build_register_area())
        self.vertical_split.addWidget(self._build_console())
        self.vertical_split.setStretchFactor(0, 4)
        self.vertical_split.setStretchFactor(1, 1)
        self.vertical_split.setSizes([550, 205])
        self.vertical_split.splitterMoved.connect(lambda: QTimer.singleShot(0, self._reveal_inspector))
        body_layout.addWidget(self.vertical_split, 1)
        main_layout.addWidget(body, 1)
        # Sidebar width is drag-resizable, same affordance as the registers/console divider.
        self.side_split = QSplitter(Qt.Horizontal)
        self.side_split.setStyleSheet("QSplitter { background: #EFF3F7; }")
        self.side_split.setHandleWidth(9)
        self.side_split.setChildrenCollapsible(False)
        self.side_split.addWidget(self._build_sidebar())
        self.side_split.addWidget(main)
        self.side_split.setStretchFactor(0, 0)
        self.side_split.setStretchFactor(1, 1)
        self.side_split.setSizes([360, 1180])
        root.addWidget(self.side_split)
        # No left-corner notice label: former status-bar messages go to the
        # system terminal (see _status); only counters and the version remain.
        self.counter_label = label("读取 0     写入 0     错误 0")
        self.statusBar().addPermanentWidget(self.counter_label)
        self.statusBar().addPermanentWidget(label(f"   版本 {__version__}   "))

    def _build_sidebar(self):
        side = QWidget()
        side.setObjectName("sidebar")
        # Ignored horizontally: no child (a long non-wrapping path label, for example)
        # may widen the content past the sidebar viewport - text wraps instead.
        side.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(side)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        layout.setContentsMargins(16, 23, 16, 14)
        layout.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(QIcon(str(resource_path("assets/logo.svg"))).pixmap(QSize(32, 32)))
        brand = QWidget()
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(0)
        brand_layout.addWidget(label("DevmemStudio", "brand"))
        layout.addLayout(row(logo, brand, 1, spacing=9))
        layout.addSpacing(12)
        self.host = IpAddressField()
        self.host.setAccessibleName("设备主机")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setFixedWidth(110)
        layout.addLayout(row(field("主板地址", self.host), field("端口", self.port)))
        self.user = QLineEdit()
        self.user.setAccessibleName("用户名")
        self.password, password_box = password_field("密码")
        self.password.setPlaceholderText("SSH 登录密码")
        self.password.setAccessibleName("SSH 密码")
        layout.addLayout(row(field("用户名", self.user), password_box))
        self.remember = QCheckBox("记住密码")
        self.remember.setToolTip("将登录密码保存在本机 registers.json 中。")
        self.remember.setMinimumHeight(30)
        layout.addWidget(self.remember)
        self.connect_button = button("SSH连接", self.toggle_connection, "primary", "connect")
        self.connect_button.setMinimumHeight(35)
        layout.addWidget(self.connect_button)
        layout.addWidget(divider())
        layout.addSpacing(2)
        self.serial_port = ComboBox()
        self.serial_port.setAccessibleName("本机端口")
        self.serial_port.setMinimumWidth(140)
        self._serial_devices = []
        for device, description in list_serial_ports():
            self.serial_port.addItem(self._serial_port_label(device, description), device)
            self._serial_devices.append(device)
        self.serial_baud = ComboBox()
        for baud in ("115200", "57600", "38400", "19200", "9600"):
            self.serial_baud.addItem(baud)
        self.serial_baud.setFixedWidth(110)
        layout.addLayout(row(field("本机端口", self.serial_port), field("波特率", self.serial_baud)))
        self.serial_user = QLineEdit()
        self.serial_user.setPlaceholderText("留空自动登录")
        self.serial_user.setAccessibleName("用户名")
        self.serial_password, serial_password_box = password_field("密码")
        self.serial_password.setPlaceholderText("留空无需密码")
        self.serial_password.setAccessibleName("密码")
        layout.addLayout(row(field("用户名", self.serial_user), serial_password_box))
        self.serial_remember = QCheckBox("记住密码")
        self.serial_remember.setToolTip("将串口登录密码保存在本机 registers.json 中。")
        self.serial_remember.setMinimumHeight(30)
        layout.addWidget(self.serial_remember)
        self.serial_connect_button = button("serial连接", self._toggle_serial, "primary", "connect")
        self.serial_connect_button.setMinimumHeight(35)
        layout.addWidget(self.serial_connect_button)
        layout.addSpacing(16)
        self.import_button = button("导入 top", self.import_top, "demo", "export")
        self.import_button.setToolTip("解析 emcc mix top 文件，按 REG_SPACE_BIAS 加载组件目录。连接设备后禁用，请先断开再导入。")
        self.import_component_button = button("导入组件", self.import_component, "demo", "refresh")
        self.import_component_button.setToolTip("RTL 组件修改后重新解析寄存器定义（免重新打包）：选组件文件夹导单个，"
                                                "选上级目录（如 emcc_ctrl）批量导入其下全部组件。")
        layout.addLayout(row(label("组件目录", "sideCaption"), 1, self.import_component_button, self.import_button))
        self.component_search = QLineEdit()
        self.component_search.setPlaceholderText("搜索设备名 / 类型 / 地址")
        self.component_search.setClearButtonEnabled(True)
        self.component_search.addAction(icon("search", size=16), QLineEdit.LeadingPosition)
        self.component_search.textChanged.connect(self._filter_components)
        layout.addWidget(self.component_search)
        self.component_tree = QTreeWidget()
        self.component_tree.setHeaderHidden(True)
        self.component_tree.setMinimumWidth(300)
        self.component_tree.setMinimumHeight(220)
        # Flat single-column layout: the selection bar spans the full row with no
        # separate branch area, which QSS cannot paint consistently (torn selection).
        self.component_tree.setRootIsDecorated(False)
        self.component_tree.setIndentation(0)
        self.component_tree.itemClicked.connect(self._component_clicked)
        self.component_tree.setExpandsOnDoubleClick(False)
        self.component_tree.itemExpanded.connect(self._update_group_marker)
        self.component_tree.itemCollapsed.connect(self._update_group_marker)
        layout.addWidget(self.component_tree, 1)
        self.top_summary = label("导入 top 后按类型列出组件", "sideCaption")
        self.top_summary.setWordWrap(True)
        layout.addWidget(self.top_summary)
        self.base_field = QLineEdit()
        self.base_field.setObjectName("mono")
        self.base_field.editingFinished.connect(self._base_edited)
        self.base_field.setToolTip("未找到 components_param.vh；可手动修改基地址（十六进制）。")
        layout.addLayout(row(label("基地址", "sideCaption"), 1, self.base_field))
        layout.addStretch(0)  # no slack here: the component tree above absorbs it all
        scroll = _SidebarScroll()
        scroll.setWidget(side)
        return scroll

    def _build_header(self):
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(64)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 12, 24, 12)
        layout.addStretch()
        self.target_label = label("等待建立设备会话", "muted")
        layout.addWidget(self.target_label)
        layout.addSpacing(10)
        # Two independent status badges so each connection's state is visible at a glance.
        self.ssh_badge = label("", "badge")
        layout.addWidget(self.ssh_badge)
        self.com_badge = label("", "badge")
        layout.addWidget(self.com_badge)
        # Freeze each badge at its widest state text so 离线/已连接 toggles never
        # change the badge size (which would shift the header layout).
        ssh_variants = [self._badge_dot(s) + t for s, t in
                        (("offline", "SSH 离线"), ("connected", "SSH 已连接"),
                         ("demo", "SSH 演示"), ("connecting", "SSH 连接中"))]
        com_variants = [self._badge_dot("offline") + "serial 离线",
                        self._badge_dot("connected") + "serial 已连接"]
        for badge, variants in ((self.ssh_badge, ssh_variants), (self.com_badge, com_variants)):
            for text in variants:
                badge.setText(text)
                badge.setMinimumWidth(max(badge.minimumWidth(), badge.sizeHint().width()))
            badge.setFixedWidth(badge.minimumWidth())
        self._update_connection_badge()
        layout.addSpacing(8)
        layout.addWidget(button("使用指南", lambda: show_help(self), "flat", "help"))
        return header

    def _build_register_area(self):
        panel = QWidget()
        panel.setMinimumHeight(290)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.module_title = label("寄存器映射", "sectionTitle")
        self.register_count = label("", "muted")
        self.poll_check = QCheckBox("自动读取")
        self.poll_check.toggled.connect(self._poll_toggled)
        self.interval = ComboBox()
        for text, value in (("0.1 s", 100), ("0.2 s", 200), ("0.5 s", 500), ("1 s", 1000),
                            ("2 s", 2000), ("5 s", 5000), ("10 s", 10000)):
            self.interval.addItem(text, value)
        self.interval.setFixedWidth(88)
        self.interval.currentIndexChanged.connect(lambda _i: self._interval_edited())
        self.interval.setToolTip("自动读取周期")
        self.read_all_button = button("读取全部", self.read_all, "primary", "read")
        self.write_all_button = button("批量写入", self.write_all, None, "write")
        self.remote_buttons.extend([self.read_all_button, self.write_all_button])
        self.module_badge = label("", "badge")
        self.export_button = button("导出快照", self.export_snapshot, None, "export")
        layout.addLayout(row(self.module_title, self.register_count, 1, self.module_badge, self.poll_check,
                             self.interval, self.write_all_button, self.read_all_button,
                             self.export_button, spacing=10))
        self.horizontal_split = QSplitter(Qt.Horizontal)
        self.horizontal_split.setHandleWidth(10)
        self.horizontal_split.setChildrenCollapsible(False)
        table_card = QFrame()
        table_card.setObjectName("card")
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(0, 0, 0, 5)
        table_layout.setSpacing(0)
        filters = QWidget()
        filters.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        filters_layout = QHBoxLayout(filters)
        filters_layout.setContentsMargins(12, 9, 12, 9)
        filters_layout.setSpacing(3)
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.view_buttons = {}
        for key in top_import.VIEW_ORDER:
            tab = button(top_import.VIEW_LABELS[key], self._view_changed, "tab")
            tab.setCheckable(True)
            tab.setToolTip(VIEW_TOOLTIPS[key])
            self.view_group.addButton(tab)
            self.view_buttons[key] = tab
            filters_layout.addWidget(tab)
        self.view_buttons["basic"].setChecked(True)
        filters_layout.addStretch()
        self.access_filter = ComboBox()
        self.access_filter.addItems(["全部权限", "只读", "可读写"])
        self.access_filter.setFixedWidth(112)
        self.access_filter.currentIndexChanged.connect(self.filter_rows)
        filters_layout.addWidget(self.access_filter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称 / 偏移")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        self.search.setMinimumWidth(145)
        self.search.addAction(icon("search", size=16), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self.filter_rows)
        filters_layout.addWidget(self.search)
        table_layout.addWidget(filters)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["寄存器", "偏移", "权限", "当前值 · HEX", "当前值 · DEC", "待写入 · HEX", "读取"])
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        header = self.table.horizontalHeader()
        alignments = (Qt.AlignLeft, Qt.AlignRight, Qt.AlignHCenter, Qt.AlignRight,
                      Qt.AlignRight, Qt.AlignRight, Qt.AlignHCenter)
        for index, width in enumerate((125, 72, 55, 126, 115, 123, 48)):
            self.table.setColumnWidth(index, width)
            self.table.horizontalHeaderItem(index).setTextAlignment(alignments[index] | Qt.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemChanged.connect(self._table_edited)
        table_layout.addWidget(self.table, 1)
        self.empty_label = label("没有匹配的寄存器，请调整搜索或筛选条件。", "muted")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setMinimumHeight(80)
        self.empty_label.hide()
        table_layout.addWidget(self.empty_label)
        self.table_note = label("  选中行查看位状态 · 双击待写入值编辑", "muted")
        self.table_note.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        table_layout.addWidget(self.table_note)
        self.horizontal_split.addWidget(table_card)
        self.horizontal_split.addWidget(self._build_inspector())
        self.horizontal_split.setStretchFactor(0, 1)
        self.horizontal_split.setStretchFactor(1, 0)
        self.horizontal_split.setSizes([875, 285])
        layout.addWidget(self.horizontal_split, 1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        # Fixed-height slot: task start/finish must not shift the splitter either.
        progress_holder = QWidget()
        progress_layout = QHBoxLayout(progress_holder)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.addWidget(self.progress)
        progress_holder.setFixedHeight(3)
        layout.addWidget(progress_holder)
        return panel

    def _build_inspector(self):
        self.inspector = QFrame()
        self.inspector.setObjectName("inspector")
        self.inspector.setMinimumWidth(270)
        self.inspector.setMaximumWidth(350)
        outer = QVBoxLayout(self.inspector)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        self.inspector_scroll = scroll
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(17, 6, 17, 10)
        layout.setSpacing(6)
        self.inspector_context = QWidget()
        context_layout = QVBoxLayout(self.inspector_context)
        context_layout.setContentsMargins(17, 12, 17, 6)
        context_layout.setSpacing(6)
        self.reg_name = label("—", "registerName")
        self.reg_name.setToolTip("当前检查的寄存器")
        self.access_badge = label("只读", "badge")
        context_layout.addLayout(row(self.reg_name, 1, self.access_badge))
        self.reg_address = label("—", "mono")
        self.copy_address_button = button("", self.copy_address, "flat", "copy")
        self.copy_address_button.setToolTip("复制完整地址")
        self.copy_address_button.setFixedWidth(28)
        context_layout.addLayout(row(self.reg_address, 1, self.copy_address_button))
        context_layout.addWidget(divider())
        layout.addWidget(label("当前值 · HEX", "eyebrow"))
        self.hex_value = label("—", "value")
        self.hex_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.hex_value)
        self.dec_value = label("尚未读取", "muted")
        self.dec_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.dec_value)
        self.updated_label = label("等待设备数据", "muted")
        layout.addWidget(self.updated_label)
        layout.addWidget(divider())
        self.inspect_modes = QButtonGroup(self)
        self.write_mode = button("写入设置", self._inspector_mode_changed, "tab")
        self.bit_mode = button("解析与位状态", self._inspector_mode_changed, "tab")
        for control in (self.write_mode, self.bit_mode):
            control.setCheckable(True)
            self.inspect_modes.addButton(control)
        self.bit_mode.setChecked(True)
        layout.addLayout(row(self.write_mode, self.bit_mode, 1))
        self.decoded_view = DecodedFieldsView()
        layout.addWidget(self.decoded_view)
        self.bit_heading = label("位 31 → 0       ● 1   ○ 0", "eyebrow")
        layout.addWidget(self.bit_heading)
        self.bits = BitView()
        layout.addWidget(self.bits)
        self.decoded_label = label("", "muted")
        self.decoded_label.setWordWrap(True)
        self.decoded_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.decoded_label)
        self.editor_panel = QWidget()
        editor = QVBoxLayout(self.editor_panel)
        editor.setContentsMargins(0, 0, 0, 0)
        editor.setSpacing(8)
        self.editor_title = label("待写入值", "muted")
        self.width_combo = ComboBox()
        self.width_combo.addItems(["8", "16", "32", "64"])
        self.width_combo.setFixedWidth(80)
        self.width_combo.currentTextChanged.connect(self._editor_changed)
        editor.addLayout(row(self.editor_title, 1, label("访问位宽", "muted"), self.width_combo))
        self.preset_combo = ComboBox()
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        editor.addWidget(self.preset_combo)
        self.write_input = QLineEdit()
        self.write_input.setObjectName("mono")
        self.write_input.setPlaceholderText("待写入值")
        self.write_input.textChanged.connect(self._editor_changed)
        self.format_combo = ComboBox()
        self.format_combo.addItem("HEX", "H")
        self.format_combo.addItem("DEC", "D")
        self.format_combo.setFixedWidth(88)
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        editor.addLayout(row(self.write_input, self.format_combo))
        self.command_preview = label("—", "mono")
        self.command_preview.setWordWrap(True)
        self.command_preview.setStyleSheet("font-family:Consolas;font-size:10px;color:#7B8EA1;")
        self.command_preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        editor.addWidget(self.command_preview)
        self.write_button = button("写入并回读", self.write_selected, "primary", "write")
        self.write_button.setMinimumHeight(34)
        self.remote_buttons.append(self.write_button)
        layout.addWidget(self.editor_panel)
        self.readonly_note = label("只读寄存器 · 支持读取与状态检查", "muted")
        self.readonly_note.setWordWrap(True)
        layout.addWidget(self.readonly_note)
        self.read_selected_button = button("读取", self.read_selected, None, "read")
        self.remote_buttons.append(self.read_selected_button)
        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(self.inspector_context)
        outer.addWidget(scroll, 1)
        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 8, 12, 12)
        footer_layout.setSpacing(6)
        footer_layout.addWidget(self.read_selected_button)
        footer_layout.addWidget(self.write_button, 1)
        outer.addWidget(footer)
        return self.inspector

    def _build_console(self):
        card = QFrame()
        card.setObjectName("card")
        card.setMinimumHeight(183)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(13, 9, 13, 11)
        layout.setSpacing(7)
        self.log_filter = ComboBox()
        self.log_filter.addItems(["全部日志", "仅错误", "仅命令"])
        self.log_filter.setFixedWidth(118)
        self.log_filter.currentIndexChanged.connect(self._render_logs)
        self.follow_log = QCheckBox("跟随")
        self.follow_log.setChecked(True)
        # Terminal source selector: log=板端 sunny.log 流, ssh=SSH 命令日志,
        # serial=串口命令日志, system=软件自身运行日志.
        self.console_source_combo = ComboBox()
        for key, text in (("log", "tail log"), ("ssh", "ssh"), ("com", "serial"), ("system", "system")):
            self.console_source_combo.addItem(text, key)
        self.console_source_combo.setFixedWidth(118)
        self.console_source_combo.setToolTip("终端来源：tail log=板端 sunny.log 流（SSH tail），ssh=SSH 会话日志，serial=串口会话日志，system=软件运行日志。")
        self.console_source_combo.currentIndexChanged.connect(self._change_console_source)
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("查找日志…")
        self.log_search.setClearButtonEnabled(True)
        self.log_search.setMinimumWidth(240)
        self.log_search.setMaximumWidth(280)
        self.log_search.addAction(icon("search", size=16), QLineEdit.LeadingPosition)
        self.log_search.returnPressed.connect(lambda: self._find_in_log(forward=True))
        # Match counter right of the search box: current match / total matches.
        self.log_match_count = label("", "muted")
        self.log_match_count.setMinimumWidth(46)
        self.log_match_count.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        self.log_search.textChanged.connect(self._log_search_changed)
        self.log_prev = button("", lambda: self._find_in_log(forward=False), "flat", "up")
        self.log_prev.setToolTip("上一个匹配")
        self.log_next = button("", lambda: self._find_in_log(forward=True), "flat", "down")
        self.log_next.setToolTip("下一个匹配")
        self.log_wrap = QCheckBox("换行")
        self.log_wrap.setChecked(True)
        self.log_wrap.toggled.connect(lambda on: self.console.setLineWrapMode(QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap))
        self.bit_upload_button = button("上传bit", self.open_bit_upload, "flat", "export")
        self.bit_upload_button.setToolTip("选择或拖入 .bit 文件，重命名为 sunny_fpga.bit 上传到 /run/media/sda；原文件备份为 sunny_fpga.bit_时间戳。")
        self.bit_rollback_button = button("回退bit", self.open_bit_rollback, "flat", "refresh")
        self.bit_rollback_button.setToolTip("列出板端 /run/media/sda 下的 sunny_fpga.bit* 备份，选择后把所选版本恢复为 sunny_fpga.bit（当前版本先备份为时间戳）。")
        self.log_download_button = button("下载log", self.download_board_log, "flat", "export")
        self.log_download_button.setToolTip("下载板端 /run/media/sda/sunny.log 到本地（选择保存位置）。")
        self.reboot_button = button("重启设备", self.reboot, "flat", "refresh")
        # upload/download/reboot need only a live session, not a selected register component
        layout.addLayout(row(label("会话终端", "sectionTitle"),
                             self.log_filter, self.console_source_combo,
                             self.log_search, self.log_match_count,
                             self.log_prev, self.log_next,
                             self.log_wrap, self.follow_log, 1,
                             self.bit_upload_button, self.bit_rollback_button,
                             self.log_download_button,
                             self.reboot_button,
                             button("导出", self.export_logs, "flat", "export"),
                             button("清空", self.clear_logs, "flat"), spacing=6))
        # The console itself is the shell: session views (ssh/serial) expose an
        # editable prompt line at the bottom; tail log / system stay read-only.
        self.console = TerminalView(self.send_command)
        self.console.setObjectName("console")
        self.console.setMaximumBlockCount(3000)
        self.console.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        layout.addWidget(self.console, 1)
        return card

    def _load_config_fields(self):
        self.host.setText(self.cfg["host"])
        self.port.setValue(self.cfg["port"])
        self.user.setText(self.cfg["username"])
        self.password.setText(self.cfg["password"])
        self.remember.setChecked(bool(self.cfg["remember_password"]))
        self.base_field.setText(self.cfg["base"])
        # 串口字段：下拉不可编辑，仅能选枚举项；波特率选预置值。
        index = self.serial_port.findData(self.cfg.get("serial_port", ""))
        if index >= 0:
            self.serial_port.setCurrentIndex(index)
        index = self.serial_baud.findText(str(self.cfg.get("serial_baud", 115200)))
        if index >= 0:
            self.serial_baud.setCurrentIndex(index)
        self.serial_user.setText(self.cfg.get("serial_username", ""))
        self.serial_password.setText(self.cfg.get("serial_password", ""))
        self.serial_remember.setChecked(bool(self.cfg.get("remember_serial_password", False)))
        # 终端来源不记忆：每次打开固定在 system，仅本次会话内手动切换。
        idx = self.console_source_combo.findData("system")
        self.console_source_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.console_source = self.console_source_combo.currentData() or "system"
        saved = self.cfg.get("poll_interval", 1000)
        index = self.interval.findData(saved)
        if index < 0:
            # 旧配置里的自定义周期：选最接近的预设。
            presets = [self.interval.itemData(i) for i in range(self.interval.count())]
            index = min(range(len(presets)), key=lambda i: abs(presets[i] - saved), default=0)
        self.interval.setCurrentIndex(index)

    def _shortcuts(self):
        for sequence, callback in (("F5", self.read_all), ("Ctrl+F", self._focus_log_search),
                                   ("Ctrl+L", self.console.setFocus), ("Ctrl+Shift+S", self.export_snapshot),
                                   ("Esc", self.cancel_task)):
            QShortcut(QKeySequence(sequence), self, activated=callback)

    def _focus_log_search(self):
        """Ctrl+F: seed the search box with the terminal's current selection."""
        cursor = self.console.textCursor()
        if cursor.hasSelection():
            # selectedText uses U+2020 for newlines; first line only, trimmed.
            text = cursor.selectedText().split("†")[0].strip()
            if text:
                self.log_search.setText(text)   # find-as-you-type picks it up
        self.log_search.setFocus()
        self.log_search.selectAll()

    def import_top(self):
        if self._closing or self._busy or self.connected:
            return
        start = self.cfg.get("top_path") or ""
        path, _ = QFileDialog.getOpenFileName(self, "选择 top 文件", start, "SystemVerilog (*.sv *.v);;所有文件 (*)")
        if not path:
            return
        answer = QMessageBox.question(self, "确认导入 top",
                                      f"即将导入：{path}\n\n若 top 与连接设备不一致，操作寄存器可能导致主板卡死！\n确认导入？",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.load_top(Path(path))

    def import_component(self):
        """Re-parse component register definitions at runtime (RTL changed, no rebuild).

        A picked folder that carries ps_rw_pl_reg_* imports that one component; any other
        folder is scanned recursively and imports every component found beneath it."""
        if self._closing or self._busy:
            return
        start = self.cfg.get("top_path") or ""
        folder = QFileDialog.getExistingDirectory(
            self, "选择组件文件夹，或上级目录（批量导入其下全部组件）", start)
        if not folder:
            return
        folder = Path(folder)
        if any(folder.glob("ps_rw_pl_reg_*.sv")) or any(folder.glob("ps_rw_pl_reg_*.v")):
            targets = [folder]
        else:
            targets = component_parse.find_component_folders(folder)
            if not targets:
                QMessageBox.warning(self, "导入组件失败",
                                    "所选目录及其子目录中未找到组件（需含 ps_rw_pl_reg_*.sv/.v）。")
                return
        results, failures = [], []
        for target in targets:
            try:
                entry, info = component_parse.parse_component_folder(target)
                results.append((target, entry, info))
            except (ValueError, OSError) as exc:
                failures.append((target.name, str(exc)))
        if not results:
            QMessageBox.warning(self, "导入组件失败", "\n".join(f"{name}：{reason}" for name, reason in failures))
            self.append_log("ERROR", f"导入组件失败：{failures}")
            return
        summary = f"解析成功 {len(results)} 个组件，失败 {len(failures)} 个。"
        if failures:
            summary += "\n失败项：\n" + "\n".join(f"{name}：{reason}" for name, reason in failures[:8])
        if len(results) == 1:
            target, entry, _ = results[0]
            summary = f"类型 {self._canonical_type_key(target)}：解析到 {len(entry['registers'])} 个寄存器。"
        answer = QMessageBox.question(self, "确认导入组件",
                                      f"{summary}\n来源：{folder}\n\n确认导入？",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if self.type_catalog is None:
            self.type_catalog = {"schema": 6, "types": {}}
        updated_keys = set()
        try:
            overrides = user_data_dir() / "component_overrides"
            overrides.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            overrides = None
            self.append_log("ERROR", f"组件定义保存目录不可用（本次会话内仍生效）：{exc}")
        for target, entry, info in results:
            key = self._canonical_type_key(target)
            self.type_catalog["types"][key] = entry
            updated_keys.add(key)
            if overrides is not None:
                try:
                    (overrides / f"{key}.json").write_text(
                        json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as exc:
                    self.append_log("ERROR", f"组件定义保存失败（{key}，本次会话内仍生效）：{exc}")
            self.append_log("SYSTEM", f"已导入组件类型 {key}：{len(entry['registers'])} 项寄存器（{target}）。")
            for message in info["warnings"]:
                self.append_log("SYSTEM", f"组件：{message}")
        self._rebuild_component_tree()
        if self.active_component and self.active_component["module_type"] in updated_keys:
            self._last_context = None
            self.rebuild_registers()

    @staticmethod
    def _component_display_name(comp) -> str:
        """Human-readable component name for the tree/title: the top label when
        present, otherwise the instance name (never blank)."""
        return str(comp.get("label") or comp.get("instance") or comp.get("module_type") or "")

    def _canonical_type_key(self, folder: Path) -> str:
        """Map an imported folder onto the catalog's type key (decode folders may rename types)."""
        if self.type_catalog:
            marker = f"/{folder.name}/"
            for key, entry in self.type_catalog["types"].items():
                if marker in f"/{entry.get('decode_file', '')}/" or \
                        str(entry.get("imported_from", "")) == str(folder):
                    return key
        return folder.name

    def load_top(self, path, quiet=False):
        """Parse a mix top file and rebuild the sidebar component catalog."""
        path = Path(path)
        try:
            info = top_import.parse_top(top_import.read_text_resilient(path))
        except OSError as exc:
            message = f"读取 top 文件失败：{exc}"
            if not quiet:
                QMessageBox.warning(self, "导入失败", message)
            self.append_log("ERROR", message)
            return False
        if not info["components"]:
            message = f"未在 {path.name} 中找到 flow_comp 组件块。"
            if not quiet:
                QMessageBox.warning(self, "导入失败", message)
            self.append_log("ERROR", message)
            return False
        self.top_info = {**info, "path": str(path)}
        self.cfg["top_path"] = str(path)
        self._schedule_save()
        self._apply_rtl_base(path)
        self._rebuild_component_tree()
        self._filter_components(self.component_search.text())
        self.active_component = None
        self._pending_component = None
        self.view = "basic"
        self.view_buttons["basic"].setChecked(True)
        self.rebuild_registers()
        self.append_log("SYSTEM", "组件目录已更新，请在左侧选择组件。")
        active = [item for item in info["components"] if not item["disabled"]]
        disabled = len(info["components"]) - len(active)
        summary = f"{len(active)} 组件 · {len({item['module_type'] for item in active})} 类型"
        if disabled:
            summary += f" · {disabled} 禁用"
        self.top_summary.setText(f"{summary}\n{path}")
        self.top_summary.setToolTip(str(path))
        prefix = "已重新加载 top" if quiet else "导入 top"
        self.append_log("SYSTEM", f"{prefix} {path}：{len(info['components'])} 个组件块，{len(info['warnings'])} 条警告。")
        for message in info["warnings"]:
            self.append_log("SYSTEM", f"top：{message}")
        if self.type_catalog is None:
            self.append_log("SYSTEM", "未找到组件寄存器目录（component_catalog.json），组件将使用通用回退寄存器表。")
        return True

    def _apply_rtl_base(self, path):
        """Set the base address from components_param.vh; otherwise leave it editable."""
        param = top_import.find_components_param(path)
        address = None
        if param is not None:
            try:
                address = top_import.parse_base_address(top_import.read_text_resilient(param))
            except OSError:
                address = None
        if address is not None:
            self.rtl_base = address
            self.base_field.setText(f"0x{address:08x}")
            self.base_field.setEnabled(False)
            self.base_field.setToolTip(f"由 {param.name} 的 PL_CFG_BASE_ADDR 自动设定。")
            self.append_log("SYSTEM", f"已根据 {param.name} 将基地址设为 0x{address:08X}。")
        else:
            self.rtl_base = None
            self.base_field.setEnabled(True)
            self.base_field.setToolTip("未找到 components_param.vh；可手动修改基地址（十六进制）。")
            self.append_log("SYSTEM", "未找到 components_param.vh，使用当前基地址，可在左侧修改。")

    def _base_edited(self):
        if self._closing:
            return
        try:
            base = validated_address(self.base_field.text())
        except ValueError:
            self.base_field.setProperty("invalid", True)
            restyle(self.base_field)
            self._status("基地址须为 0x00000000–0xFFFFFFFF 的十六进制数。")
            self.address_valid = False
            self.poll_check.setChecked(False)
            self._refresh_controls()
            return
        self.base_field.setProperty("invalid", False)
        restyle(self.base_field)
        self.base_field.setText(f"0x{base:08x}")
        self.cfg["base"] = f"0x{base:08x}"
        self._schedule_save()
        if not self._busy:
            self.rebuild_registers()

    def base_value(self):
        return self.rtl_base if self.rtl_base is not None else validated_address(self.base_field.text())

    def _rebuild_component_tree(self):
        self.component_tree.clear()
        components = self.top_info["components"] if self.top_info else []
        try:
            full_base = self.base_value()
        except ValueError:
            full_base = None
        groups = {}
        for comp in components:
            groups.setdefault(comp["module_type"], []).append(comp)
        for module_type, items in groups.items():
            registers, exact = top_import.registers_for(self.type_catalog, module_type)
            note = f"精确寄存器 {len(registers)} 项" if exact else "无精确寄存器表，使用通用回退（≈）"
            group = QTreeWidgetItem([f"    {module_type} ({len(items)})"])
            group.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            group.setData(0, Qt.UserRole + 1, module_type)
            group.setForeground(0, QColor("#8097AA"))
            group.setToolTip(0, f"{module_type}\n{note}")
            for comp in items:
                # Address lives in the tooltip and the title row; keep list items name-only.
                # Leading spaces indent level-2 items without a branch column, so the
                # selection bar still spans the full row (no torn selection).
                item = QTreeWidgetItem(["        " + self._component_display_name(comp)])
                item.setData(0, Qt.UserRole, comp)
                if comp["disabled"]:
                    item.setFlags(Qt.NoItemFlags)
                    item.setForeground(0, QColor("#5A6B7A"))
                    item.setToolTip(0, "该组件在 top 中已被注释禁用。")
                else:
                    full = f"全地址 0x{full_base + comp['bias']:08X}" if full_base is not None else "基地址未解析"
                    item.setToolTip(0, f"{comp['label']}\n类型 {module_type} · 例化 {comp['instance']}\n"
                                          f"组件地址 {comp['address']} · {full}")
                group.addChild(item)
            self.component_tree.addTopLevelItem(group)
        self._highlight_component()

    @staticmethod
    def _update_group_marker(item):
        """Keep the group count in sync after search-driven expansion changes."""
        module_type = item.data(0, Qt.UserRole + 1)
        if not module_type:
            return
        item.setText(0, f"    {module_type} ({item.childCount()})")

    def _filter_components(self, text):
        if not hasattr(self, "component_tree"):
            return
        query = str(text).strip().lower()
        for index in range(self.component_tree.topLevelItemCount()):
            group = self.component_tree.topLevelItem(index)
            visible = 0
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                comp = child.data(0, Qt.UserRole) or {}
                haystack = " ".join(str(comp.get(key, "")) for key in
                                    ("label", "code", "module_type", "address", "instance"))
                match = not query or query in haystack.lower()
                child.setHidden(not match)
                visible += bool(match)
            group.setHidden(visible == 0)
            group.setExpanded(bool(query) and visible > 0)

    def _component_clicked(self, item, _column):
        comp = item.data(0, Qt.UserRole)
        if isinstance(comp, dict):
            self.select_component(comp)
        elif not item.parent():
            item.setExpanded(not item.isExpanded())   # single-click toggles type groups

    def select_component(self, comp):
        if self._closing or comp.get("disabled"):
            return
        if self._busy:
            # Queue the switch; applied when the running task finishes (see _task_finished).
            self._pending_component = comp
            self._status(f"当前任务完成后切换到 {self._component_display_name(comp)}。")
            return
        self.active_component = comp
        self._highlight_component()
        self.rebuild_registers()
        self.append_log("SYSTEM", f"已选择组件 {self._component_display_name(comp)}（{comp['module_type']} @ {comp['address']}）。")
        if self._can_operate():
            self.read_all()

    @property
    def component_mode(self):
        return self.active_component is not None

    def _view_changed(self, *args):
        key = next((name for name, tab in self.view_buttons.items() if tab.isChecked()), "all")
        if key != self.view:
            self.view = key
            self.filter_rows()
            if self._can_operate():
                self.read_all()

    def _highlight_component(self):
        if not hasattr(self, "component_tree"):
            return
        # Color only - toggling bold would resize the text and tear the selection bar.
        for group_index in range(self.component_tree.topLevelItemCount()):
            group = self.component_tree.topLevelItem(group_index)
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                comp = child.data(0, Qt.UserRole)
                if not isinstance(comp, dict) or comp.get("disabled"):
                    continue  # disabled items keep their greyed-out look
                child.setForeground(0, QColor("#FFFFFF" if comp is self.active_component else "#C8D7E5"))

    def rebuild_registers(self):
        if not self.active_component:
            self.address_valid = False
            self._last_context = None
            self._module_start = None
            self.regs = []
            self.visible_regs = []
            self.selected = None
            self.row_buttons = []   # drop stale read-button wrappers before the table drops them
            self.table.setRowCount(0)
            self.inspector.setEnabled(False)
            self.module_title.setText("寄存器映射")
            self.module_badge.setText("")
            self.register_count.setText("")
            self.empty_label.setText("先点击左侧「导入 top」加载组件目录，再选择组件开始调试。" if not self.top_info
                                     else "从左侧「组件目录」选择一个组件开始调试。")
            self.empty_label.show()
            self.table.hide()
            self.table_note.setText("")
            self._refresh_controls()
            return
        comp = self.active_component
        try:
            base = self.base_value()
            start = validated_address(hex(base + comp["bias"]))
            registers, exact = top_import.registers_for(self.type_catalog, comp["module_type"])
            for register in registers:
                validated_address(hex(start + parse_addr(register["offset"])))
        except ValueError as exc:
            self.address_valid = False
            self.base_field.setProperty("invalid", True)
            restyle(self.base_field)
            self._status(str(exc))
            self.poll_check.setChecked(False)
            self._refresh_controls()
            return
        self.address_valid = True
        if self.rtl_base is None:
            self.base_field.setProperty("invalid", False)
            restyle(self.base_field)
        self._module_start = start
        context = ("top", comp["module_type"], comp["bias"], base)
        self.module_title.setText(f"{self._component_display_name(comp)} · 寄存器映射")
        self.module_badge.setText(comp["module_type"] + ("" if exact else " ≈"))
        self.module_badge.setToolTip("组件类型来自导入的 top；≈ 表示使用通用回退寄存器表")
        if context == self._last_context:
            self._refresh_controls()
            return
        self._last_context = context
        self._updating = True
        self.selected = None
        self.regs = copy.deepcopy(sorted(registers, key=lambda r: parse_addr(r["offset"])))
        self.cache_key = f"top:{comp['module_type']}@0x{comp['bias']:04x}"
        if not exact:
            self.append_log("SYSTEM", f"未找到 {comp['module_type']} 的精确寄存器表，已使用通用回退。")
        meta = top_import.type_metadata(self.type_catalog, comp["module_type"])
        cache = self.cfg.get("write_cache", {}).get(self.cache_key, {})
        if not isinstance(cache, dict):
            cache = {}
        self.row_buttons = []
        self.table.setRowCount(len(self.regs))
        for index, reg in enumerate(self.regs):
            reg.update(_address=start + parse_addr(reg["offset"]), _value=None, _updated="", _error="", _readback_failed=False,
                       _row=index, _access_width=access_width(reg), _fmt="D", _source="未读取", _target="")
            note = meta["notes"].get(reg["name"]) or meta["debug_notes"].get(reg["name"])
            if note:
                reg["_note"] = note
            if reg["name"] in ("A_BHV_ID", "B_BHV_ID", "C_BHV_ID"):
                behaviors = meta["behaviors"].get(reg["name"][0], [])
                if behaviors and not reg.get("readonly"):
                    reg.setdefault("aliases", [{"name": item["name"], "value": str(item["value"])}
                                               for item in behaviors])
            presets = reg.get("aliases") or reg.get("buttons") or []
            initial = "0x1" if reg.get("action") else (presets[0]["value"] if presets else reg.get("value", "0x0"))
            reg["_draft"] = f"{parse_int(initial) or 0}"
            remembered = cache.get(reg["name"])
            if isinstance(remembered, dict) and not reg.get("action"):
                previous = str(remembered.get("v", reg["_draft"]))
                match = re.search(r"\((0[xX][0-9a-fA-F]+|\d+)\)\s*$", previous)
                if match:
                    reg["_draft"] = f"{parse_int(match.group(1))}"
                else:
                    reg["_draft"] = previous
                    reg["_fmt"] = remembered.get("f", "D") if remembered.get("f") in ("H", "D") else "D"
                if remembered.get("w") in (8, 16, 32, 64):
                    reg["_access_width"] = remembered["w"]
            mode = "RO" if reg.get("readonly") else "WO" if reg.get("write_only") else "ACT" if reg.get("action") else "RW"
            display = self._display_name(reg)
            texts = [display, f'0x{parse_addr(reg["offset"]):03X}', mode, "—", "—", "—", ""]
            for col, text in enumerate(texts):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if col == 5 and not (reg.get("readonly") or reg.get("action") or presets):
                    item.setFlags(item.flags() | Qt.ItemIsEditable)
                if col in (1, 3, 4, 5):
                    item.setFont(QFont("Consolas", 10))
                item.setTextAlignment(self.table.horizontalHeaderItem(col).textAlignment())
                tooltip = f'{display} · {register_group(reg)}\n全地址 0x{reg["_address"]:08X}\n字段长度 {reg.get("width", 32)} 位'
                if reg["name"].startswith("PARAM") and not reg.get("signal"):
                    tooltip += "\n未接线：顶层例化中已注释，读回值无意义。" if reg.get("unwired") else \
                               "\n预留：寄存器已接线但组件内未使用（PS 可自由读写）。"
                if reg.get("_note"):
                    tooltip += f'\n{reg["_note"]}'
                item.setToolTip(tooltip)
                if col == 2:
                    item.setForeground(QColor("#A56E20" if mode == "ACT" else "#718397" if mode in ("RO", "WO") else "#2463DC"))
                self.table.setItem(index, col, item)
            read_button = button("", lambda checked=False, r=reg: self.read_register(r), "rowRead", "read")
            read_button.setIconSize(QSize(15, 15))
            read_button.setToolTip(f'读取 {reg["name"]} · 0x{reg["_address"]:08X}')
            self.table.setCellWidget(index, 6, read_button)
            self.row_buttons.append(read_button)
            self._update_table_row(reg)
        self._updating = False
        self.search.clear()
        self.filter_rows()
        self.cfg["base"] = f"0x{base:08x}"
        self._refresh_controls()
        self._schedule_save()

    @staticmethod
    def _display_name(reg):
        """Register name plus its real RTL signal, or the wiring state for PARAM slots."""
        if reg.get("signal"):
            return f'{reg["name"]} · {reg["signal"]}'
        if reg["name"].startswith("PARAM"):
            return f'{reg["name"]} · {"未接线" if reg.get("unwired") else "预留"}'
        return reg["name"]

    @staticmethod
    def _dec_display(reg, value):
        """Decimal text: two's complement for registers fed by signed wires."""
        if value is None:
            return "—"
        if reg.get("signed"):
            width = reg.get("width", 32) or 32
            if width < 8 or width not in (8, 16, 32, 64):
                width = 32
            if value >= 1 << (width - 1):
                return str(value - (1 << width))
        return str(value)

    @staticmethod
    def _pulse_scaled_label(reg):
        """Derived-row label for pulse-domain values: two's-complement value / PARAM4.

        Position-family signals (r_pf_abspos, rserv_target/step_pulse, rcfg_pos_*)
        show mm | °; speed/accel/decel config signals (rcfg_*spd/acc/dec) show
        per-second scaled values."""
        signals = {part.split("[")[0] for part in reg.get("signal", "").split("/")}
        pulse_positions = {"r_pf_abspos", "rserv_target_pulse", "rserv_step_pulse"}
        if any(s in pulse_positions or s.startswith("rcfg_pos") for s in signals):
            return "实际值 (mm | °)"
        scaled = [s for s in signals if s.startswith("rcfg_")
                  and any(key in s for key in ("spd", "acc", "dec"))]
        if not scaled:
            return None
        return "实际值 (mm/s² | °/s²)" if any("acc" in s or "dec" in s for s in scaled) else "实际值 (mm/s | °/s)"

    def _param4_value(self):
        """Scaled pulse count per unit; None when PARAM4 is unread or not present."""
        for reg in self.regs:
            if reg["name"] == "PARAM4" and reg.get("_value") is not None:
                return reg["_value"]
        return None

    def _actual_value(self, reg, value):
        """Actual value in units = two's-complement register value / param4, or None."""
        divisor = self._param4_value()
        if divisor is None or divisor == 0:
            return None
        return int(self._dec_display(reg, value)) / divisor

    @staticmethod
    def _format_actual(number):
        if number is None:
            return "—"
        return f"{number:.6f}".rstrip("0").rstrip(".")

    def _update_table_row(self, reg):
        index = reg["_row"]
        value = reg["_value"]
        previous = self._updating
        self._updating = True
        self.table.item(index, 3).setText("回读失败" if reg.get("_readback_failed") else "读取错误" if reg["_error"] else f"0x{value:08X}" if value is not None else "—")
        self.table.item(index, 3).setForeground(QColor("#C44848" if reg["_error"] else "#2463DC"))
        self.table.item(index, 4).setText(self._dec_display(reg, value) if not reg["_error"] else "—")
        if reg["_error"]:
            self.table.item(index, 3).setToolTip(reg["_error"])
        else:
            definitions = self._field_defs(reg)
            fields = self._decoded_values(reg, value, definitions)
            self.table.item(index, 3).setToolTip("\n".join(f"{key}={val}" for key, val in fields.items()) or "HEX 实测值")
        if reg.get("readonly"):
            text = "—"
        elif reg.get("action"):
            text = "触发 0x1"
        else:
            try:
                val = write_value(reg["_draft"], reg["_fmt"], reg["_access_width"])
                text = f"0x{val:X}"
                for preset in reg.get("aliases", []) + reg.get("buttons", []):
                    if parse_int(preset["value"]) == val:
                        text = f'{preset["name"]} · {text}'
                        break
            except ValueError:
                text = "输入无效"
        self.table.item(index, 5).setText(text)
        self.table.item(index, 5).setForeground(QColor("#768B9E" if reg.get("readonly") else "#526E92"))
        self._updating = previous

    def filter_rows(self, *args):
        if not hasattr(self, "table"):
            return
        access = self.access_filter.currentIndex()
        query = self.search.text().strip().lower()
        visible = []
        for index, reg in enumerate(self.regs):
            match = (top_import.reg_in_view(reg, self.view) and
                     (access == 0 or access == 1 and reg.get("readonly") or
                      access == 2 and not reg.get("readonly")))
            haystack = f'{reg["name"]} {reg.get("signal", "")} {register_group(reg)} {reg["offset"]} 0x{reg["_address"]:08x} {parse_addr(reg["offset"]):03x}'
            hidden = not match or bool(query and query not in haystack.lower())
            self.table.setRowHidden(index, hidden)
            if not hidden:
                visible.append(index)
        self.visible_regs = [self.regs[index] for index in visible]
        if self.active_component:
            address = f" · 全地址 0x{self._module_start:08X}" if self._module_start is not None else ""
            self.register_count.setText(f"{len(visible)} / {len(self.regs)} 项{address}")
        else:
            self.register_count.setText("")
        self.empty_label.setVisible(not visible)
        self.table.setVisible(bool(visible))
        if visible:
            if self.table.currentRow() not in visible:
                self.table.selectRow(visible[0])
            else:
                self._selection_changed()
        else:
            self.selected = None
            self.inspector.setEnabled(False)
            if self.active_component:
                self.empty_label.setText(f"此类型在「{top_import.VIEW_LABELS[self.view]}」视图没有寄存器，可切换视图或搜索。")
        self.table_note.setText(f"  {len(visible)} 项可见 · 选中行查看解析与位状态 · 双击待写入值编辑" if visible else "  0 项可见")

    def _selection_changed(self):
        if self._updating:
            return
        index = self.table.currentRow()
        self.selected = self.regs[index] if 0 <= index < len(self.regs) and not self.table.isRowHidden(index) else None
        self.inspector.setEnabled(self.selected is not None)
        if not self.selected:
            return
        reg = self.selected
        self._updating = True
        self.reg_name.setText(reg["name"])
        self.reg_address.setText(f'0x{reg["_address"]:08X}')
        self.access_badge.setText("只读" if reg.get("readonly") else "写-only" if reg.get("write_only")
                                  else "动作" if reg.get("action") else "读写")
        self.write_mode.setEnabled(not reg.get("readonly", False))
        self.bit_mode.setChecked(True)   # 所有寄存器选中默认落在“解析与位状态”
        self.write_button.setVisible(not reg.get("readonly", False))
        self.readonly_note.setVisible(reg.get("readonly", False))
        self.width_combo.setCurrentText(str(reg["_access_width"]))
        self._last_fmt = reg["_fmt"]
        self.format_combo.setCurrentIndex(0 if reg["_fmt"] == "H" else 1)
        self.write_input.setText(reg["_draft"])
        self.write_input.setReadOnly(bool(reg.get("action")))
        presets = reg.get("aliases") or reg.get("buttons") or []
        self.preset_combo.clear()
        self.preset_combo.addItem("选择预设 / 自定义值", None)
        for preset in presets:
            self.preset_combo.addItem(f'{preset["name"]}   ({preset["value"]})', parse_int(preset["value"]))
        try:
            value = write_value(reg["_draft"], reg["_fmt"], reg["_access_width"])
            self.preset_combo.setCurrentIndex(max(0, self.preset_combo.findData(value)))
        except ValueError:
            pass
        self.preset_combo.setVisible(bool(presets))
        self.write_button.setText("触发并回读" if reg.get("action") else "写入并回读")
        self._updating = False
        self._inspector_mode_changed()
        self._show_measurement()
        self._update_command_preview()
        self._refresh_controls()

    def _inspector_mode_changed(self):
        if not self.selected:
            return
        self.inspector_scroll.widget().setMinimumHeight(0)
        show_bits = self.bit_mode.isChecked()
        has_decode = bool(self._has_field_decode(self.selected))
        self.editor_panel.setVisible(not show_bits and not self.selected.get("readonly", False))
        self.bits.setVisible(show_bits)
        self.bit_heading.setVisible(show_bits)
        self.decoded_view.setVisible(show_bits and has_decode)
        self.decoded_label.setVisible(show_bits and (bool(self.selected["_error"]) or not has_decode))
        if show_bits:
            self.inspector_scroll.verticalScrollBar().setValue(0)
        QTimer.singleShot(0, self._reveal_inspector)

    def _reveal_inspector(self):
        if self._closing or not self.selected:
            return
        target = (self.decoded_view if self.decoded_view.isVisible() else
                  self.write_input if self.editor_panel.isVisible() else None)
        if target is None:
            return
        content = self.inspector_scroll.widget()
        content.setMinimumHeight(0)
        content.layout().activate()
        available = self.inspector_scroll.viewport().height()
        natural_height = content.layout().minimumSize().height()
        target_bottom = target.mapTo(content, target.rect().bottomRight()).y()
        if target_bottom + 8 <= available:
            self.inspector_scroll.verticalScrollBar().setValue(0)
            return
        # Align the scroll position with a complete section, leaving room below
        # the fields/editor instead of cutting through the measured value above it.
        anchor = max(0, self.write_mode.y() - 6)
        content.setMinimumHeight(max(natural_height, anchor + available))
        content.layout().activate()
        self.inspector_scroll.verticalScrollBar().setValue(anchor)
        self.inspector_scroll.ensureWidgetVisible(target, 0, 8)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "inspector_scroll"):
            QTimer.singleShot(0, self._reveal_inspector)

    def _field_defs(self, reg):
        """Field decode for the inspector: per-bit layout parsed from the RTL concat
        hookup first, then the static table; DEBUG state history is excluded when the
        register is actually a bit snapshot."""
        scaled_label = self._pulse_scaled_label(reg)
        if scaled_label:
            return ((scaled_label, 0, 1, "DEC"),)   # high<low → derived row
        fields = reg.get("fields")
        if fields:
            return tuple((item["name"], item["low"] + item["width"] - 1, item["low"], "DEC")
                         for item in fields)
        definitions = REGISTER_FIELDS.get(reg["name"], ())
        if definitions and reg["name"].startswith("DEBUG_REG") and str(reg.get("_note", "")).startswith("位快照"):
            return ()
        return definitions

    def _has_field_decode(self, reg):
        return bool(self._field_defs(reg))

    def _decoded_values(self, reg, value, definitions):
        """Live per-field values for the inspector (decimal unless a static HEX field)."""
        if value is None:
            return {}
        scaled_label = self._pulse_scaled_label(reg)
        if scaled_label is not None:
            return {scaled_label: self._format_actual(self._actual_value(reg, value))}
        static = format_decoded_fields(reg["name"], value) if not reg.get("fields") else {}
        values = {}
        for name, high, low, radix in definitions:
            if name in static:
                values[name] = static[name]
                continue
            masked = (value >> low) & ((1 << (high - low + 1)) - 1)
            values[name] = f"0x{masked:02X}" if radix == "HEX" else str(masked)
        return values

    def _show_measurement(self):
        reg = self.selected
        if not reg:
            return
        value = reg["_value"] if not reg["_error"] else None
        self.hex_value.setText(f"0x{value:08X}" if value is not None else "—")
        self.dec_value.setText(f"DEC  {int(self._dec_display(reg, value)):,}" if value is not None and not reg["_error"] else "写入已完成，回读失败" if reg.get("_readback_failed") else "读取失败" if reg["_error"] else "尚未读取")
        when = reg["_updated"]
        self.updated_label.setText((f"更新于 {when}" + (" · 离线缓存" if not self.connected else "")) if when else "等待设备数据")
        self.bits.set_value(value)
        definitions = self._field_defs(reg)
        self.decoded_view.set_fields(definitions, self._decoded_values(reg, value, definitions))
        self.decoded_view.setVisible(self.bit_mode.isChecked() and bool(definitions))
        note = f' · {reg["_note"]}' if reg.get("_note") else ""
        self.decoded_label.setText(reg["_error"] or f'{register_group(reg)} · 字段长度 {reg.get("width", 32)} 位{note}')
        self.decoded_label.setVisible(self.bit_mode.isChecked() and (bool(reg["_error"]) or not definitions))

    def _remember_reg(self, reg):
        self.cfg.setdefault("write_cache", {}).setdefault(self.cache_key, {})[reg["name"]] = {
            "v": reg["_draft"], "f": reg["_fmt"], "w": reg["_access_width"]}
        self._schedule_save()

    def _table_edited(self, item):
        if self._updating or item.column() != 5:
            return
        reg = self.regs[item.row()]
        reg["_draft"] = item.text().strip()
        reg["_fmt"] = "D"
        self._remember_reg(reg)
        self._update_table_row(reg)
        self._selection_changed()

    def _editor_changed(self, *args):
        if self._updating or not self.selected:
            return
        reg = self.selected
        reg["_draft"] = self.write_input.text()
        reg["_fmt"] = self.format_combo.currentData()
        reg["_access_width"] = int(self.width_combo.currentText())
        try:
            value = write_value(reg["_draft"], reg["_fmt"], reg["_access_width"])
            self._updating = True
            self.preset_combo.setCurrentIndex(max(0, self.preset_combo.findData(value)))
        except ValueError:
            pass
        finally:
            self._updating = False
        self._remember_reg(reg)
        self._update_table_row(reg)
        self._update_command_preview()

    def _preset_changed(self, index):
        if self._updating or not self.selected:
            return
        value = self.preset_combo.currentData()
        if value is not None:
            self.write_input.setText(f"{value:X}" if self.format_combo.currentData() == "H" else str(value))

    def _format_changed(self, *args):
        if self._updating or not self.selected:
            return
        new = self.format_combo.currentData()
        try:
            value = write_value(self.write_input.text(), self._last_fmt, int(self.width_combo.currentText()))
        except ValueError:
            self._updating = True
            self.format_combo.setCurrentIndex(0 if self._last_fmt == "H" else 1)
            self._updating = False
            self._status("请先修正写入值，再切换进制。")
            return
        self._last_fmt = new
        self.write_input.setText(f"{value:X}" if new == "H" else str(value))
        self._editor_changed()

    def _update_command_preview(self):
        if not self.selected:
            return
        reg = self.selected
        try:
            value = write_value(reg["_draft"], reg["_fmt"], reg["_access_width"])
            self.command_preview.setText(write_command(reg["_address"], reg["_access_width"], value))
            self.write_input.setProperty("invalid", False)
            self.write_button.setEnabled(self.connected and not self._busy and self.address_valid)
        except ValueError as exc:
            self.command_preview.setText(str(exc))
            self.write_input.setProperty("invalid", True)
            self.write_button.setEnabled(False)
        restyle(self.write_input)

    def copy_address(self):
        if self.selected:
            QApplication.clipboard().setText(f'0x{self.selected["_address"]:08X}')
            self._status("寄存器地址已复制。")

    def _refresh_controls(self):
        enabled = self.connected and not self._busy and getattr(self, "address_valid", False)
        for control in self.remote_buttons + getattr(self, "row_buttons", []):
            try:
                control.setEnabled(enabled)
            except RuntimeError:
                # 重建表格/切换组件后，旧行按钮的 C++ 对象可能已被 Qt 销毁；
                # 跳过并把失效包装器从列表中清除，而不是让后续刷新崩溃。
                stale = getattr(self, "row_buttons", [])
                if control in stale:
                    stale.remove(control)
        # Commands and the independent log stream do not depend on register addresses.
        # Terminal input lives in the console itself; availability follows the source:
        # serial views need the serial link, everything else rides the SSH session.
        for control in (self.reboot_button, self.bit_upload_button, self.bit_rollback_button,
                        self.log_download_button):
            control.setEnabled(self.connected and not self._busy and not self._closing)
        for control in (self.host, self.port, self.user, self.password, self.remember):
            control.setEnabled(not self.connected and not self._busy)
        # Serial side is fully independent: its fields follow _serial_busy only,
        # so SSH tasks never lock the serial connection UI (and vice versa).
        for control in (self.serial_port, self.serial_baud, self.serial_user, self.serial_password,
                        self.serial_remember):
            control.setEnabled(not self.serial_connected and not self._serial_busy)
        self.serial_connect_button.setEnabled(not self._serial_busy)
        for control in (self.component_tree, self.component_search, self.import_component_button,
                        self.access_filter, self.search, *self.view_buttons.values()):
            control.setEnabled(not self._busy and not self._closing)
        # A top mismatched with the live device can hang the board; import only while disconnected.
        self.import_button.setEnabled(not self._busy and not self._closing and not self.connected)
        if self.rtl_base is None:
            self.base_field.setEnabled(not self._busy and not self._closing)
        self.connect_button.setEnabled(not self._busy or self._task_kind == "connect")
        self.poll_check.setEnabled(self.connected and getattr(self, "address_valid", False))
        self.editor_panel.setEnabled(not self._busy and getattr(self, "address_valid", False))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers if self._busy else
                                  QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.read_selected_button.setEnabled(enabled and self.selected is not None)
        if self.selected:
            self._update_command_preview()

    def _style_session_button(self, btn, connected, on_text, off_text):
        """Connect buttons: blue 'connect' while offline, red 'disconnect' once linked."""
        btn.setText(on_text if connected else off_text)
        btn.setObjectName("danger" if connected else "primary")
        btn.setIcon(icon("disconnect", "#B64242") if connected else icon("connect", "#FFFFFF"))
        restyle(btn)

    def _set_connection(self, connected):
        self.connected = connected
        # DemoSession is an internal simulator used by tests and offline acceptance.
        self.demo = isinstance(self.session, DemoSession)
        self._update_connection_badge()
        self._refresh_target_label()
        self._style_session_button(self.connect_button, connected, "断开SSH", "SSH连接")
        if connected and self.demo:
            self._status("演示模式 · 当前数据来自本地模拟")
        if not connected:
            self.poll_check.setChecked(False)
            self._stop_stream()
        else:
            # SSH just came up: tail sunny.log into the log buffer (background,
            # regardless of the visible source).
            self._ensure_log_stream()
        self._show_measurement()
        self._refresh_controls()

    def _reset_measurements(self):
        for reg in self.regs:
            reg.update(_value=None, _updated="", _error="", _readback_failed=False, _source="未读取", _target="")
            self._update_table_row(reg)
        self.read_count = self.write_count = self.error_count = 0
        self._update_counts()
        self._show_measurement()

    def toggle_connection(self):
        if self._busy:
            # A connect in flight: the same button doubles as the cancel control.
            if self._task_kind == "connect":
                self.append_log("SYSTEM", "已请求取消 SSH 连接。")
                self.session.close()   # closes the handshake socket → connect raises at once
            return
        if self.connected:
            self.disconnect()
            return
        if not self.host.text().strip() or not self.user.text().strip():
            self._status("请填写设备主机地址和用户名。")
            self.host.setFocus()
            return
        request = (self.host.text().strip(), self.port.value(), self.user.text().strip(),
                   self.password.text(), CONNECT_TIMEOUT)
        self._connect_device(request)

    def _connect_device(self, request, approved_change=None):
        if self._busy or self._closing:
            return
        host, port, user, password, timeout = request
        if approved_change and (approved_change.hostname, approved_change.port) != (host, port):
            raise ValueError("确认的主机密钥与连接目标不一致。")
        self._pending_host_key_change = None
        self.session.close()
        self.session = session = SshSession(self._log_emit("ssh"))
        self._reset_measurements()
        self.save_settings()
        # The button becomes a red 'cancel' control while the handshake runs.
        self._style_session_button(self.connect_button, True, "取消连接", "SSH连接")
        self.ssh_badge.setText(self._badge_dot("connecting") + "SSH 连接中")
        self.append_log("SYSTEM", f"正在连接 {user}@{host}:{port}。")
        def connect(progress):
            if approved_change:
                session.replace_host_key(approved_change)
            try:
                session.connect(host, port, user, password, timeout)
            except HostKeyChangedError as change:
                return change
            return True
        def done(result):
            if isinstance(result, HostKeyChangedError):
                self._set_connection(False)
                self.append_log("ERROR", str(result))
                self._pending_host_key_change = (result, request)
                return
            self._set_connection(True)
            self.append_log("SUCCESS", "SSH 连接成功。")
        self._run_task(connect, done, "SSH连接", "connect")

    def _confirm_host_key_change(self, change, request):
        if self._closing or self._busy or self.connected or self.cancel.is_set():
            return
        current = (self.host.text().strip(), self.port.value(), self.user.text().strip())
        if current != request[:3]:
            return
        dialog = HostKeyDialog(change, self)
        if dialog.exec() == QDialog.Accepted and not self._closing:
            self._connect_device(request, approved_change=change)
        elif not self._closing:
            self.append_log("SYSTEM", "已取消主机密钥更新。")

    def disconnect(self):
        self.cancel.set()
        self.session.close()
        self._set_connection(False)
        self._pending_component = None
        self.append_log("SYSTEM", "会话已断开。当前显示保留为最近一次读取结果。")

    def _serial_baud_value(self):
        text = str(self.serial_baud.currentText()).strip()
        try:
            return max(1200, min(10000000, int(float(text))))
        except ValueError:
            return 115200

    def _toggle_serial(self):
        if self._serial_busy:
            # A connect in flight: the same button doubles as the cancel control.
            self._serial_cancel_requested = True
            self.append_log("SYSTEM", "已请求取消 serial 连接。")
            if self._task_serial:
                self._task_serial.cancel_connect()
            return
        if self.serial_connected:
            self.disconnect_serial()
            return
        port = self._selected_serial_port()
        if not port:
            self._status("请选择 serial 端口。")
            self.serial_port.setFocus()
            return
        request = (port, self._serial_baud_value(), self.serial_user.text().strip(),
                   self.serial_password.text(), CONNECT_TIMEOUT)
        self._connect_serial(request)

    def _serial_port_label(self, device, description):
        """Pick text for a listed serial entry: the full board name (e.g.
        ``USB Serial Port (COM6)``), falling back to the device when Windows
        gives no description. The device stays as the item data for the
        actual connection."""
        return (description or "").strip() or device

    def _selected_serial_port(self):
        """The device name for the picked port: the combo's data (COMx) when a
        listed entry is chosen, else the typed text."""
        data = self.serial_port.currentData()
        return (data if isinstance(data, str) and data else self.serial_port.currentText()).strip()

    def _connect_serial(self, request):
        """Connect serial on its own worker, outside the global busy gate:
        SSH (and register tasks) stay fully usable while serial handshakes."""
        if self._serial_busy or self._closing:
            return
        port, baud, user, password, timeout = request
        self.serial.close()
        session = SerialSession(self._log_emit("com"))
        self._task_serial = session
        self.save_settings()
        self._serial_busy = True
        # Same 3px indeterminate bar as the SSH handshake (see _update_progress).
        self.progress.setRange(0, 0)
        self.progress.show()
        # The button becomes a red 'cancel' control while the handshake runs.
        self._style_session_button(self.serial_connect_button, True, "取消连接", "serial连接")
        for control in (self.serial_port, self.serial_baud, self.serial_user,
                        self.serial_password, self.serial_remember):
            control.setEnabled(False)
        self.append_log("SYSTEM", f"正在连接 serial {port} @ {baud} bps。")
        def connect(progress):
            session.connect(port, baud, user, password, timeout)
            return True
        def done(result):
            if self._closing:
                return
            self._serial_busy = False
            self._task_serial = None
            self.serial = session
            self.serial_connected = True
            self._serial_device = port
            self._reflect_serial(True)
            self.append_log("SUCCESS", "serial 连接成功。")
        def failed(message):
            if self._closing:
                return
            self._serial_busy = False
            if self._task_serial:
                self._task_serial.close()
                self._task_serial = None
            self.serial_connected = False
            self._serial_device = None
            self._reflect_serial(False)
            cancelled = self._serial_cancel_requested
            self._serial_cancel_requested = False
            self.append_log("SYSTEM" if cancelled else "ERROR", message)
        worker = Worker(connect)
        self._serial_worker = worker   # keep the reference: pool.start alone lets GC drop it
        worker.signals.result.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.finished.connect(self._serial_worker_finished)
        self.pool.start(worker)

    def _serial_worker_finished(self):
        self._serial_busy = False
        self._serial_worker = None
        if self._closing:
            QTimer.singleShot(0, self.close)
        else:
            self._update_progress()

    def disconnect_serial(self):
        self.serial.close()
        self.serial_connected = False
        self._serial_device = None
        self._reflect_serial(False)
        self.append_log("SYSTEM", "serial 会话已断开。")

    def _reflect_serial(self, connected):
        self.serial_connected = connected
        self._style_session_button(self.serial_connect_button, connected, "断开连接", "serial连接")
        self._update_connection_badge()
        self._refresh_target_label()
        self._refresh_controls()

    def _refresh_target_label(self):
        """Top-right SSH session summary; the serial state lives in the badges.
        The waiting hint only shows when nothing at all is connected."""
        if self.connected and self.demo:
            text = "本地模拟设备"
        elif self.connected:
            text = f"SSH {self.user.text()}@{self.host.text()}:{self.port.value()}"
        elif self.serial_connected:
            text = ""
        else:
            text = "等待建立设备会话"
        self.target_label.setText(text)

    def _update_connection_badge(self):
        """Refresh the two independent SSH/serial status badges in the top-right.
        Each shows its own connection state so neither hides the other. The dot
        is a fixed-size rich-text glyph whose color alone changes, so it renders
        the same size in every state."""
        if self.connected and self.demo:
            ssh_text, ssh_state = "SSH 演示", "demo"
        elif self.connected:
            ssh_text, ssh_state = "SSH 已连接", "connected"
        else:
            ssh_text, ssh_state = "SSH 离线", "offline"
        self.ssh_badge.setText(self._badge_dot(ssh_state) + ssh_text)
        self.ssh_badge.setProperty("state", ssh_state)
        restyle(self.ssh_badge)
        com_state = "connected" if self.serial_connected else "offline"
        self.com_badge.setText(self._badge_dot(com_state) + ("serial 已连接" if self.serial_connected else "serial 离线"))
        self.com_badge.setProperty("state", com_state)
        restyle(self.com_badge)

    @staticmethod
    def _badge_dot(state):
        """Fixed-size status dot (rich-text span; only the color varies by state)."""
        colors = {"connected": "#157767", "demo": "#B57518", "connecting": "#2463DC", "offline": "#8395A8"}
        return f'<span style="color:{colors.get(state, "#8395A8")};font-size:13px">●</span>&nbsp;&nbsp;'

    def open_bit_upload(self):
        if self._closing or not self.connected or self._busy:
            return
        if self.bit_dialog is None:
            self.bit_dialog = BitUploadDialog(self)
            self.bit_dialog.upload_requested.connect(self._start_bit_upload)
        if self.bit_dialog.isMinimized():
            self.bit_dialog.showNormal()
        else:
            self.bit_dialog.show()
        self.bit_dialog.raise_()
        self.bit_dialog.activateWindow()

    def _start_bit_upload(self, local_path, remote_dir):
        if self._closing or self._busy or not self.connected:
            return
        if not str(local_path).lower().endswith(".bit"):
            QMessageBox.warning(self.bit_dialog, "文件类型错误", "选中文件不是bit文件，请选择 .bit 文件。")
            return
        if not Path(local_path).is_file():
            QMessageBox.warning(self.bit_dialog, "上传失败", f"找不到文件：{local_path}")
            return
        try:
            bit_date = datetime.fromtimestamp(Path(local_path).stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            bit_date = "未知"
        answer = QMessageBox.question(self.bit_dialog, "确认上传bit",
                                      f"{local_path}\n文件日期：{bit_date}\n\n确定上传？",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.bit_dialog.upload_button.setEnabled(False)
        self.bit_dialog.set_reset()
        self.bit_dialog.status.setText("正在备份旧文件并上传，请勿断开连接。")
        self._bit_last_paint = 0.0
        session = self.session

        def task(progress):
            def report(done, total):
                progress({"current": done, "total": total})
            session.upload_bitfile(local_path, remote_dir, "sunny_fpga.bit", progress=report)
            return True

        def done(_result):
            self.bit_dialog.set_done()
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            self.bit_dialog.status.setText(f"已上传为 {remote_dir}/sunny_fpga.bit；原文件备份为 sunny_fpga.bit_{stamp}。")
            self.bit_dialog.upload_button.setEnabled(True)
            self.append_log("SUCCESS", f"bit file 上传完成：{Path(local_path).name} -> {remote_dir}/sunny_fpga.bit")

        def failed(message):
            self.bit_dialog.set_reset()
            self.bit_dialog.status.setText("上传失败：" + message)
            self.bit_dialog.upload_button.setEnabled(True)
            self.append_log("ERROR", f"bit file 上传失败：{message}")

        def finished():
            self._busy = False
            self._next_poll = time.monotonic() + self._poll_interval_ms() / 1000
            self._refresh_controls()

        worker = Worker(task)
        worker.signals.result.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.progress.connect(self._bit_progress)
        worker.signals.finished.connect(finished)
        self._active_worker = worker   # keep a reference or Qt may drop the queued signals
        self._busy = True
        self._task_kind = "upload"
        self._refresh_controls()
        self.pool.start(worker)

    @Slot(object)
    def _bit_progress(self, data):
        current, total = data.get("current", 0), data.get("total", 0)
        if total <= 0 or self._closing or not self.bit_dialog:
            return
        percent = min(100, int(current * 100 / total))
        # SFTP callbacks arrive far faster than the display needs; throttle to ~10 Hz
        # (and always fire at 100%) so the tween advances smoothly instead of stepping.
        now = time.monotonic()
        if percent < 100 and now - self._bit_last_paint < 0.1:
            return
        self._bit_last_paint = now
        self.bit_dialog.set_percent(percent)

    def open_bit_rollback(self):
        if self._closing or not self.connected or self._busy:
            return
        if self.bit_rollback_dialog is None:
            self.bit_rollback_dialog = BitRollbackDialog(self)
            self.bit_rollback_dialog.rollback_requested.connect(self._start_bit_rollback)
        self.bit_rollback_dialog.set_reset()
        if self.bit_rollback_dialog.isMinimized():
            self.bit_rollback_dialog.showNormal()
        else:
            self.bit_rollback_dialog.show()
        self.bit_rollback_dialog.raise_()
        self.bit_rollback_dialog.activateWindow()
        self._refresh_bit_backups()

    def _refresh_bit_backups(self):
        if self._closing or not self.connected or self._busy:
            return
        session = self.session
        dialog = self.bit_rollback_dialog
        dialog.set_busy(True)
        dialog.status.setText("正在读取板端 bit 备份列表…")

        def task(progress):
            return session.list_bit_backups()

        def done(entries):
            if self._closing or not dialog:
                return
            dialog.set_reset()
            dialog.populate(entries, f"共 {len(entries)} 个 bit 文件：当前版本 + {len(entries) - 1} 个备份（双击可直接回退）。"
                             if entries else "板端没有可回退的 bit 备份。")
            self.append_log("INFO", f"读取到 {len(entries)} 个板端 bit 文件。")

        def failed(message):
            if self._closing or not dialog:
                return
            dialog.set_reset()
            dialog.set_busy(False)
            dialog.status.setText("读取备份列表失败：" + message)
            self.append_log("ERROR", f"读取 bit 备份列表失败：{message}")

        def finished():
            if self._closing:
                return
            self._busy = False
            self._active_worker = None
            self._refresh_controls()

        worker = Worker(task)
        worker.signals.result.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.finished.connect(finished)
        self._active_worker = worker
        self._busy = True
        self._task_kind = "operation"
        self._refresh_controls()
        self.pool.start(worker)

    def _start_bit_rollback(self, backup_name):
        if self._closing or self._busy or not self.connected:
            return
        dialog = self.bit_rollback_dialog
        if not dialog:
            return
        answer = QMessageBox.question(self, "确认回退",
                                      f"将把当前 sunny_fpga.bit 备份为 sunny_fpga.bit_时间戳，"
                                      f"然后把 {backup_name} 恢复为 sunny_fpga.bit。\n\n是否继续？",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        session = self.session
        dialog.set_busy(True)
        dialog.set_reset()
        dialog.status.setText(f"正在回退到 {backup_name} …")

        def task(progress):
            session.rollback_bit(backup_name, progress=lambda done, total: progress({"current": done, "total": total}))
            return backup_name

        def done(name):
            if self._closing or not dialog:
                return
            dialog.set_done()
            dialog.status.setText(f"已回退：{name} 恢复为 sunny_fpga.bit（原当前版本已备份）。")
            self.append_log("SUCCESS", f"bit 回退完成：{name} -> sunny_fpga.bit")

        def failed(message):
            if self._closing or not dialog:
                return
            dialog.set_reset()
            dialog.set_busy(False)
            dialog.status.setText("回退失败：" + message)
            self.append_log("ERROR", f"bit 回退失败：{message}")

        def finished():
            if self._closing:
                return
            self._busy = False
            self._active_worker = None
            self._refresh_controls()
            self._refresh_bit_backups()   # reload the list after a successful or failed rollback

        worker = Worker(task)
        worker.signals.result.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.progress.connect(self._rollback_progress)
        worker.signals.finished.connect(finished)
        self._active_worker = worker
        self._busy = True
        self._task_kind = "upload"
        self._refresh_controls()
        self.pool.start(worker)

    @Slot(object)
    def _rollback_progress(self, data):
        if self._closing or not self.bit_rollback_dialog:
            return
        current, total = data.get("current", 0), data.get("total", 100)
        percent = min(100, int(current * 100 / total)) if total > 0 else 0
        self.bit_rollback_dialog.set_percent(percent)

    def download_board_log(self):
        if self._closing or not self.connected or self._busy:
            return
        default_name = f"sunny-{datetime.now():%Y%m%d-%H%M%S}.log"
        path, _ = QFileDialog.getSaveFileName(self, "保存板端日志", default_name, "日志文件 (*.log);;所有文件 (*)")
        if not path:
            return
        session = self.session
        remote_path = DEFAULT_LOG_PATH

        def task(progress):
            def report(done, total):
                progress({"current": done, "total": total})
            session.download_file(remote_path, path, progress=report)
            return path

        def done(result):
            self.append_log("SUCCESS", f"板端日志已下载：{remote_path} -> {result}")

        def failed(message):
            self.append_log("ERROR", f"板端日志下载失败：{message}")

        self._run_task(task, done, "下载板端日志", "download")

    def _run_task(self, function, callback, description, kind="operation"):
        if self._busy or self._closing:
            return False
        self.cancel.clear()
        self._busy = True
        self._task_kind = kind
        self._task_callback = callback
        self.progress.setRange(0, 0)
        self.progress.show()
        self._refresh_controls()
        worker = Worker(function)
        self._active_worker = worker
        worker.signals.result.connect(self._task_result)
        worker.signals.failed.connect(self._task_failed)
        worker.signals.progress.connect(self._task_progress)
        worker.signals.finished.connect(self._task_finished)
        self.pool.start(worker)
        return True

    @Slot(object)
    def _task_result(self, result):
        if not self._closing and self._task_callback:
            self._task_callback(result)

    @Slot(str)
    def _task_failed(self, message):
        if self._closing:
            return
        # Task failures are software-level events: they land in the system log;
        # user-initiated cancels are not errors.
        self.append_log("SYSTEM" if "已取消" in message else "ERROR", message)
        self.poll_check.setChecked(False)
        if not self.session.alive:
            self._set_connection(False)

    @Slot(object)
    def _task_progress(self, data):
        if self._closing:
            return
        reg = data.get("register")
        if reg is not None:
            reg["_error"] = data.get("error", "")
            reg["_readback_failed"] = bool(data.get("written") and reg["_error"])
            if data.get("written") or "value" in data and data.get("write"):
                self.write_count += 1
            if "value" in data:
                reg["_value"] = data["value"]
                reg["_updated"] = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                reg["_source"] = "离线演示" if self.demo else "设备实测"
                reg["_target"] = "DEMO" if self.demo else f"{self.host.text()}:{self.port.value()}"
                self.read_count += 1
            self._update_table_row(reg)
            if reg is self.selected:
                self._show_measurement()
            self._update_counts()
        current, total = data.get("current", 0), data.get("total", 1)
        self.progress.setRange(0, total)
        self.progress.setValue(current)

    def _update_progress(self):
        """The 3px bar is shared by SSH tasks and the serial handshake: it stays
        visible while either runs. A running task owns range/value; the serial
        handshake alone gets the indeterminate reset here."""
        if self._busy:
            return
        if self._serial_busy:
            self.progress.setRange(0, 0)
            self.progress.show()
        else:
            self.progress.hide()

    @Slot()
    def _task_finished(self):
        self._busy = False
        self._task_callback = None
        self._active_worker = None
        self._update_progress()
        self._next_poll = time.monotonic() + self._poll_interval_ms() / 1000
        self._style_session_button(self.connect_button, self.connected, "断开SSH", "SSH连接")
        self._refresh_controls()
        pending, self._pending_component = self._pending_component, None
        if self._closing:
            QTimer.singleShot(0, self.close)
        elif self._pending_host_key_change:
            change, request = self._pending_host_key_change
            self._pending_host_key_change = None
            QTimer.singleShot(0, lambda: self._confirm_host_key_change(change, request))
        elif pending is not None:
            self.select_component(pending)

    def cancel_task(self):
        if self._busy:
            self.cancel.set()
            self.poll_check.setChecked(False)
            if self._task_kind == "connect":
                self.session.close()
            elif self._task_kind == "serial":
                self._task_serial.close()
                self._task_serial = None
            self.append_log("SYSTEM", "已请求停止后续操作。")

    def _can_operate(self):
        return self.connected and not self._busy and getattr(self, "address_valid", False)

    def _read_many(self, registers, quiet=False):
        if not self._can_operate() or not registers:
            return
        records = list(registers)
        session = self.session
        def task(progress):
            completed = 0
            total = len(records)
            first_error = None
            for start in range(0, total, BATCH_READ_CHUNK):
                if self.cancel.is_set():
                    break
                part = records[start:start + BATCH_READ_CHUNK]
                try:
                    # One shell round trip per chunk; per-address failures come back as errors.
                    values = session.read_many([reg["_address"] for reg in part])
                except Exception as exc:
                    progress({"register": part[0], "error": str(exc), "current": start + 1, "total": total})
                    raise
                for index, reg in enumerate(part, start + 1):
                    value, error = values.get(reg["_address"], (None, "设备未返回数据"))
                    if error:
                        progress({"register": reg, "error": error, "current": index, "total": total})
                        if first_error is None:
                            first_error = (reg, error)
                        continue
                    progress({"register": reg, "value": value, "current": index, "total": total})
                    completed += 1
            # Sweep finished but some registers failed: fail the task so polling
            # stops and the error counter/log reflect it, as single reads did.
            if first_error is not None and not self.cancel.is_set():
                reg, error = first_error
                raise CommandError(f"0x{reg['_address']:08X} 读取失败：{error}")
            return completed
        def done(count):
            message = f"读取{'已停止' if self.cancel.is_set() else '完成'} · {count} / {len(records)} 项"
            if not quiet or self.cancel.is_set():
                self.append_log("SUCCESS" if not self.cancel.is_set() else "SYSTEM", message)
        self._run_task(task, done, "读取寄存器", "read")

    def read_all(self):
        if not self.visible_regs:
            self._status("当前视图没有可读取的寄存器。" if self.active_component
                         else "请先在左侧组件目录中选择组件。")
            return
        self._read_many(self.visible_regs)

    def read_selected(self):
        if self.selected:
            self._read_many([self.selected])

    def read_register(self, register):
        self.table.selectRow(register["_row"])
        self._read_many([register])

    def _write_many(self, records):
        if not self._can_operate() or not records:
            return
        session = self.session
        # Snapshot values in the UI thread; worker threads never read GUI widgets.
        plans = [(reg, reg["_address"], reg["_access_width"], reg["_planned_value"]) for reg in records]
        batch = len(plans) > 1
        if batch:
            address_list = " ".join(f"0x{address:08x}" for _, address, _, _ in plans)
            session.log("CMD", f"写入 {len(plans)} 个寄存器 {address_list}")
        def task(progress):
            completed = 0
            for index, (reg, address, width, value) in enumerate(plans, 1):
                if self.cancel.is_set():
                    break
                try:
                    measured = session.write(address, width, value, quiet=batch)
                except Exception as exc:
                    progress({"register": reg, "error": str(exc), "current": index, "total": len(plans),
                              "write": True, "written": isinstance(exc, ReadbackError)})
                    raise
                progress({"register": reg, "value": measured, "current": index, "total": len(plans), "write": True})
                completed += 1
            return completed
        def done(count):
            message = f"写入并回读{'已停止' if self.cancel.is_set() else '完成'} · {count} / {len(plans)} 项"
            self.append_log("SUCCESS" if not self.cancel.is_set() else "SYSTEM", message)
        self._run_task(task, done, "写入寄存器", "write")

    def _prepare_write(self, reg):
        if reg.get("readonly"):
            raise ValueError("只读寄存器不支持写入。")
        value = write_value(reg["_draft"], reg["_fmt"], reg["_access_width"])
        write_command(reg["_address"], reg["_access_width"], value)
        reg["_planned_value"] = value

    def write_selected(self):
        if not self._can_operate() or not self.selected:
            return
        try:
            self._prepare_write(self.selected)
            self._write_many([self.selected])
        except ValueError as exc:
            self.append_log("ERROR", str(exc))

    def write_all(self):
        if not self._can_operate():
            return
        self.poll_check.setChecked(False)
        records = [reg for reg in self.visible_regs if not reg.get("readonly")]
        try:
            for reg in records:
                self._prepare_write(reg)
        except ValueError as exc:
            QMessageBox.warning(self, "请修正写入值", f'{reg["name"]}：{exc}')
            return
        dialog = BatchDialog(records, self)
        if dialog.exec() == QDialog.Accepted:
            self._write_many(dialog.selected())

    def _poll_interval_ms(self):
        """Auto-read interval in milliseconds from the combo; accepts typed values.

        Accepts "0.3" / "0.3 s" (seconds with decimals) or "300" / "300ms"
        (milliseconds); clamps to the fastest 0.1 s and 60 s. The typed text wins
        over the still-selected preset item so custom values take effect at once."""
        text = str(self.interval.currentText()).strip().lower()
        match = re.match(r"^(\d+(?:\.\d+)?)\s*(ms|s)?$", text)
        if match:
            number = float(match.group(1))
            unit = match.group(2)
            if unit == "ms":
                ms = number
            elif unit == "s":
                ms = number * 1000
            else:
                ms = number * 1000 if number < 60 else number   # 无单位：按秒，>=60 按毫秒
            return max(100, min(60000, int(ms)))
        data = self.interval.currentData()
        if data is not None:
            return max(100, min(60000, int(data)))
        return 1000

    def _interval_edited(self):
        self._schedule_save()

    def _poll_toggled(self, checked):
        self._next_poll = 0
        if checked:
            self._status("自动读取已开启 · 每次读取完成后等待所选间隔")

    def _poll(self):
        if self.poll_check.isChecked() and not self._closing and self._can_operate() and time.monotonic() >= self._next_poll:
            self._read_many(self.visible_regs, quiet=True)

    def _check_session(self):
        if self.connected and not self._busy and not self.session.alive:
            self.session.close()
            self._set_connection(False)
            self.append_log("ERROR", "设备连接已中断，请检查网络或设备状态后重新连接。")
        if self.serial_connected and not self._busy and not self.serial.alive:
            self.serial.close()
            self.serial_connected = False
            self._reflect_serial(False)
            self.append_log("ERROR", "serial 连接已中断，请检查串口线缆后重新连接。")
        self._refresh_serial_ports()

    def _refresh_serial_ports(self):
        """Rescan attached COM ports: pick up hot-plugs and drop the connected
        port when its device disappears from the system."""
        if self._closing or self._busy:
            return
        devices = [device for device, _ in list_serial_ports()]
        if self.serial_connected and self._serial_device and self._serial_device not in devices:
            # The plugged-in port was unplugged: drop the session cleanly.
            self.serial.close()
            self.serial_connected = False
            self._reflect_serial(False)
            self.append_log("ERROR", f"serial {self._serial_device} 已拔出，连接已断开。")
        if devices != self._serial_devices:
            current = self.serial_port.currentData()
            self.serial_port.blockSignals(True)
            self.serial_port.clear()
            for device, description in list_serial_ports():
                self.serial_port.addItem(self._serial_port_label(device, description), device)
            self._serial_devices = devices
            if current is not None and current in devices:
                self.serial_port.setCurrentIndex(self.serial_port.findData(current))
            self.serial_port.blockSignals(False)

    def send_command(self, text=""):
        """Run a shell command typed in the terminal prompt (routes by source)."""
        if self._busy:
            return
        text = (text or "").strip()
        if not text:
            return
        if self.console_source == "com":
            if not self.serial_connected:
                self._status("serial 未连接，请先连接 serial。")
                return
            session = self.serial
        else:  # ssh / log / system 视图的输入都走 SSH
            if not self.connected:
                self._status("SSH 未连接，请先连接 SSH。")
                return
            session = self.session
        def done(result):
            self._status("命令执行完成。")
        self._run_task(lambda progress: session.run(text), done, "执行命令", "command")

    def reboot(self):
        if not self.connected or self._busy:
            return
        self.poll_check.setChecked(False)
        answer = QMessageBox.question(self, "重启设备", "确认向当前设备发送 reboot？重启后需要重新连接。",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        def task(progress):
            try:
                self.session.run("reboot", timeout=3)
            finally:
                self.session.close()
        def done(_):
            self._set_connection(False)
            self.append_log("SYSTEM", "reboot 命令已发送，请等待设备重启后重新连接。")
        self._run_task(task, done, "重启设备", "reboot")

    def _ensure_log_stream(self):
        """Start tailing sunny.log whenever SSH is up (regardless of the visible
        view): the live log flows into the log buffer in the background, and the
        log source view shows it on demand. Called on SSH connect and when the
        log view is selected while the stream has died."""
        if self._closing or not self.connected or self._stream_state != "stopped":
            return
        self._start_stream(DEFAULT_LOG_PATH)

    def _start_stream(self, path):
        if self._closing or not self.connected or self._stream_state != "stopped":
            return
        self.cfg["log_path"] = path
        self._schedule_save()
        self._stream_epoch += 1
        token = self._stream_epoch
        self._stream_cancel = stop = threading.Event()
        self._stream_state = "starting"
        session = self.session
        def task(progress):
            return session.start_stream(path, lambda text: self.bridge.stream.emit(token, text),
                                        lambda: self.bridge.stream_stopped.emit(token), cancel_event=stop)
        worker = Worker(task)
        self._stream_workers[token] = worker
        worker.signals.result.connect(lambda active: self.bridge.stream_ready.emit(token, bool(active)))
        worker.signals.failed.connect(lambda message: self.bridge.stream_failed.emit(token, message))
        worker.signals.finished.connect(lambda: self.bridge.stream_worker_finished.emit(token))
        self.stream_pool.start(worker)

    def _stop_stream(self):
        self._stream_epoch += 1
        self._stream_cancel.set()
        try:
            self.session.stop_stream()
        except Exception:
            pass
        self._stream_state = "stopped"

    @Slot(int, bool)
    def _stream_ready(self, token, active):
        if not self._closing and token == self._stream_epoch:
            active = active and self._stream_finished_epoch != token
            self._stream_state = "running" if active else "stopped"
            if active and self.console_source != "log":
                self._status("sunny.log 正在打印（切到 tail log 查看）。")

    @Slot(int, str)
    def _stream_failed(self, token, message):
        if not self._closing and token == self._stream_epoch:
            self._stream_state = "stopped"
            # Show the failure in the log view (where sunny.log would render) and the SSH log.
            self._append_log_stream(f"\n启动日志失败：{message}\n")
            self.append_log("ERROR", "打印日志启动失败：" + message)

    @Slot(int)
    def _stream_worker_finished(self, token):
        self._stream_workers.pop(token, None)
        if self._closing:
            QTimer.singleShot(0, self.close)

    @Slot(int, str)
    def _stream_output(self, token, text):
        if not self._closing and token == self._stream_epoch:
            self._append_log_stream(text)

    @Slot(int)
    def _stream_stopped(self, token):
        if not self._closing and token == self._stream_epoch:
            self._stream_finished_epoch = token
            self._stream_state = "stopped"

    def _update_counts(self):
        self.counter_label.setText(f"读取 {self.read_count}     写入 {self.write_count}     错误 {self.error_count}")

    def _status(self, text):
        """Notices that used to live in the status bar's left corner; they now
        land in the system terminal."""
        self.append_log("SYSTEM", text)

    @Slot(str, str, str)
    def append_log(self, level, text, source="system"):
        if self._closing:
            return
        # source rides with the signal (sessions) or is passed explicitly by
        # direct callers. Direct calls default to the software's own log
        # ("system"): session output, the sunny.log stream and software
        # bookkeeping (handshake, imports, bit ops) each stay in their terminal.
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        text = str(text)
        record = (stamp, level, text)
        buf = self._records_for(source)
        buf.append(record)
        if len(buf) > 3000:
            del buf[:-3000]
            if source == "ssh":
                self.log_records = buf
        if level == "ERROR":
            self.error_count += 1
            self._update_counts()
        if self.file_logger:
            self.file_logger.info("[%s] %s", level, text)
        if source == self.console_source and self._log_visible(level):
            self._append_log_record(record)

    def _log_emit(self, source):
        """Per-session log callback: the session calls log(level, text); the
        adapter attaches this session's source. SYSTEM-level records (session
        handshake bookkeeping like 串口已打开/控制台已就绪) go to the system log."""
        def emit(level, text):
            self.bridge.message.emit(level, text, "system" if level == "SYSTEM" else source)
        return emit

    def _records_for(self, source):
        return {"ssh": self.log_records_ssh, "com": self.log_records_com,
                "log": self.log_records_log, "system": self.log_records_system}.get(source, self.log_records_ssh)

    def _log_visible(self, level):
        # log stream has no levels; only the level filter applies to ssh/com.
        if self.console_source == "log":
            return True
        mode = self.log_filter.currentIndex()
        return mode == 0 or mode == 1 and level == "ERROR" or mode == 2 and level == "CMD"

    def _append_log_record(self, record):
        stamp, level, text = record
        colors = {"ERROR": "#FFAAAA", "SUCCESS": "#80D7C2", "CMD": "#87B8FF", "SYSTEM": "#9CAFBD", "INFO": "#C8D7E5"}
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(colors.get(level, "#C8D7E5")))
        lines = text.rstrip("\n").splitlines() or [""]
        rendered = f"{stamp}  {level:<7} {lines[0]}\n" + "".join(f"                     {line}\n" for line in lines[1:])
        if self.console.is_interactive():
            # Session view: new records land above the prompt line.
            self.console.append_before_prompt(rendered, fmt)
        else:
            cursor = QTextCursor(self.console.document())
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(rendered, fmt)
        if self.follow_log.isChecked():
            self.console.verticalScrollBar().setValue(self.console.verticalScrollBar().maximum())

    def _append_log_stream(self, text):
        """Append raw sunny.log text to the log buffer/console (no stamp/level)."""
        if not text:
            return
        self.log_records_log.append(text)
        if len(self.log_records_log) > 5000:
            del self.log_records_log[:-5000]
        if self.console_source == "log":
            cursor = QTextCursor(self.console.document())
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(text)
            if self.follow_log.isChecked():
                self.console.verticalScrollBar().setValue(self.console.verticalScrollBar().maximum())

    def _render_logs(self, *args):
        if not hasattr(self, "console"):
            return
        self.console.clear()
        if self.console_source == "log":
            cursor = QTextCursor(self.console.document())
            cursor.movePosition(QTextCursor.End)
            cursor.insertText("".join(self.log_records_log))
            if self.follow_log.isChecked():
                self.console.verticalScrollBar().setValue(self.console.verticalScrollBar().maximum())
        else:
            for record in self._records_for(self.console_source):
                if self._log_visible(record[1]):
                    self._append_log_record(record)
        # Session terminals are interactive (prompt at the bottom); log/system views are plain.
        self.console.set_interactive(self.console_source in ("ssh", "com"))
        # The visible content changed: refresh the search match counter.
        if getattr(self, "log_search", None) and self.log_search.text():
            self._log_search_changed()

    def clear_logs(self):
        self._records_for(self.console_source).clear()
        self.console.clear()
        self.console.set_interactive(self.console_source in ("ssh", "com"))   # re-adds the prompt
        if getattr(self, "log_search", None) and self.log_search.text():
            self._log_search_changed()

    def _change_console_source(self, _index):
        source = self.console_source_combo.currentData() or "system"
        prev = self.console_source
        self.console_source = source
        if prev == source:
            return
        # The sunny.log stream follows the SSH connection, not the visible view:
        # it keeps printing into the log buffer in the background. Switching to
        # the log view only nudges a dead stream back on.
        if source == "log" and self.connected and self._stream_state == "stopped":
            self._ensure_log_stream()
        self._render_logs()
        self._refresh_controls()

    def _set_console_source(self, key):
        """Programmatically select a console source (auto-switch on connect)."""
        index = self.console_source_combo.findData(key)
        if index >= 0:
            self.console_source_combo.setCurrentIndex(index)

    def _find_in_log(self, forward=True):
        """Find next/prev match of the log-search box text in the console."""
        query = self.log_search.text()
        if not query:
            return
        doc = self.console.document()
        cursor = self.console.textCursor()
        flags = QTextDocument.FindFlags()
        if not forward:
            flags |= QTextDocument.FindBackward
        found = doc.find(query, cursor, flags)
        if found.isNull():
            # Wrap around.
            start = QTextCursor(doc) if forward else QTextCursor(doc)
            if forward:
                start.movePosition(QTextCursor.Start)
            else:
                start.movePosition(QTextCursor.End)
            found = doc.find(query, start, flags)
        if not found.isNull():
            self.console.setTextCursor(found)
            self.console.ensureCursorVisible()
        self._update_log_match_count(found)

    def _update_log_match_count(self, found):
        """Show 'current/total' for the search query, e.g. 3/17."""
        query = self.log_search.text()
        if not query:
            self.log_match_count.setText("")
            return
        text = self.console.toPlainText()
        total = text.count(query)
        if not found.isNull() and total:
            index = text.count(query, 0, found.selectionStart()) + 1
            self.log_match_count.setText(f"{index}/{total}")
        else:
            self.log_match_count.setText(f"0/{total}")

    def _log_search_changed(self, _text=None):
        """Find-as-you-type keeps the match counter live with the query."""
        if not self.log_search.text():
            self.log_match_count.setText("")
            return
        self._find_in_log(forward=True)

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出会话日志", f"session_{datetime.now():%Y%m%d%H%M%S}.log", "日志文件 (*.log)")
        if path:
            try:
                records = self._records_for(self.console_source)
                text = ("".join(self.log_records_log) if self.console_source == "log"
                        else "\n".join(f"{stamp} [{level}] {text}" for stamp, level, text in records))
                Path(path).write_text(text, encoding="utf-8")
                self._status("会话日志已导出。")
            except OSError as exc:
                QMessageBox.warning(self, "导出失败", str(exc))

    def snapshot_csv(self):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["数据来源", "设备", "模块", "寄存器", "分组", "偏移", "完整地址", "权限", "字段位数", "访问位宽",
                         "当前值HEX", "当前值DEC", "字段解析", "最后读取时间", "状态"])
        context = self.active_component["module_type"] if self.active_component else ""
        for reg in self.regs:
            value = reg["_value"]
            # 与右侧“解析与位状态”一致：位置/速度族只出实际值，不导位段。
            definitions = self._field_defs(reg)
            fields = self._decoded_values(reg, value if not reg["_error"] else None, definitions)
            writer.writerow([reg["_source"], reg["_target"], context,
                             self._display_name(reg), register_group(reg), reg["offset"], f'0x{reg["_address"]:08X}',
                             "RO" if reg.get("readonly") else "ACT" if reg.get("action") else "RW", reg.get("width", 32),
                             reg["_access_width"], f"0x{value:08X}" if value is not None else "",
                             int(self._dec_display(reg, value)) if value is not None and not reg["_error"] else "",
                             " ".join(f"{key}={val}" for key, val in fields.items()), reg["_updated"],
                             reg["_error"] or ("离线缓存" if not self.connected and value is not None else "已读取" if value is not None else "未读取")])
        return stream.getvalue()

    def export_snapshot(self):
        stem = self.active_component["module_type"] if self.active_component else "registers"
        path, _ = QFileDialog.getSaveFileName(self, "导出寄存器快照", f"registers-{stem}-{datetime.now():%Y%m%d-%H%M%S}.csv",
                                             "CSV 快照 (*.csv)")
        if path:
            try:
                Path(path).write_text(self.snapshot_csv(), encoding="utf-8-sig", newline="")
                self.append_log("SUCCESS", "寄存器快照已导出：" + path)
            except OSError as exc:
                QMessageBox.warning(self, "导出失败", str(exc))

    def _schedule_save(self):
        if hasattr(self, "save_timer") and self.persist:
            self.save_timer.start(600)

    def save_settings(self):
        if not self.persist:
            return
        self.cfg.update(host=self.host.text().strip(), port=self.port.value(), username=self.user.text().strip(),
                        password=self.password.text(), remember_password=self.remember.isChecked(),
                        poll_interval=self._poll_interval_ms(),
                        serial_port=self._serial_device or self._selected_serial_port(), serial_baud=self._serial_baud_value(),
                        serial_username=self.serial_user.text().strip(),
                        serial_password=self.serial_password.text(),
                        remember_serial_password=self.serial_remember.isChecked())
        self.cfg["window_size"] = [self.width(), self.height()]
        try:
            self.store.save(self.cfg)
        except OSError as exc:
            self.append_log("ERROR", f"设置保存失败：{exc}")

    def closeEvent(self, event):
        self._closing = True
        self.poll_timer.stop()
        self.heartbeat.stop()
        self.save_timer.stop()
        self.cancel.set()
        self._stop_stream()
        self.session.close()
        self.serial.close()
        if self._busy or self._stream_workers or self._serial_busy:
            self.setEnabled(False)
            event.ignore()
            return
        self.save_settings()
        if self.file_logger:
            for handler in self.file_logger.handlers[:]:
                handler.close()
                self.file_logger.removeHandler(handler)
        event.accept()
