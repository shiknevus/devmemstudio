# -*- coding: utf-8 -*-
"""A quiet instrument palette: graphite chassis, cool alloy surfaces and signal blue."""
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

COLORS = {"chassis": "#142635", "surface": "#FFFFFF", "alloy": "#EFF3F7",
          "line": "#DEE5EC", "ink": "#23374A", "muted": "#718397",
          "blue": "#2463DC", "teal": "#168578", "amber": "#B57518", "red": "#C44848"}

PATHS = {
    "chip": '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4M9 14v-4h6v4"/>',
    "connect": '<path d="M8 3v4m8-4v4M6 7h12v3a6 6 0 0 1-12 0V7zm6 9v5"/>',
    "disconnect": '<path d="M8 3v4m8-4v4M6 7h12v3a6 6 0 0 1-12 0V7zm6 9v5"/><path d="m4 4 16 16"/>',
    "axis": '<path d="M5 19V5m0 14h14M2 8l3-3 3 3m8 8 3 3-3 3M9 15l4-7 3 4 5-7"/>',
    "io": '<path d="M3 7h18M3 17h18"/><circle cx="8" cy="7" r="3" fill="{color}"/><circle cx="16" cy="17" r="3" fill="{color}"/>',
    "bus": '<rect x="3" y="3" width="6" height="6" rx="1"/><rect x="15" y="15" width="6" height="6" rx="1"/><path d="M6 9v9h9m3-3V6H9"/>',
    "bridge": '<rect x="2" y="7" width="6" height="10" rx="1"/><rect x="16" y="7" width="6" height="10" rx="1"/><path d="M8 10h8m-8 4h8M5 4v3m14-3v3M5 17v3m14-3v3"/>',
    "read": '<path d="M12 3v12m-4-4 4 4 4-4M4 16v4h16v-4"/>',
    "write": '<path d="M12 16V4m-4 4 4-4 4 4M4 16v4h16v-4"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    "refresh": '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
    "terminal": '<path d="m4 6 5 6-5 6m8 0h8"/>',
    "export": '<path d="M14 3h7v7m-1-6-9 9M10 4H4v16h16v-6"/>',
    "play": '<path d="m8 4 12 8-12 8z"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="1"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5m0 3h.01"/>',
    "copy": '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    "chevron": '<path d="m9 5 7 7-7 7"/>',
    "up": '<path d="m5 15 7-7 7 7"/>',
    "down": '<path d="m5 9 7 7 7-7"/>',
    "eye": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eyeOff": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/><path d="m4 4 16 16"/>',
}


def icon(name, color="#718397", size=20):
    body = PATHS.get(name, PATHS["chip"]).replace("{color}", color)
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><g fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{body}</g></svg>'
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(svg.encode()).render(painter, QRectF(0, 0, size * 2, size * 2))
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


STYLE = """
* { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 12px; color: #23374A; }
QMainWindow, QDialog { background: #EFF3F7; }
QWidget#workspace { background: #EFF3F7; }
QWidget#sidebar { background: #142635; }
QScrollArea#sidebarScroll, QScrollArea#sidebarScroll > QWidget { background: #142635; border: none; }
QScrollArea#sidebarScroll QScrollBar::handle:vertical { background: #3A5468; }
QWidget#sidebar QLabel { color: #ACBCCB; background: transparent; }
QWidget#sidebar QLabel#brand { color: white; font-family: "Microsoft YaHei UI"; font-size: 20px; font-weight: 600; }
QWidget#sidebar QLabel#sideCaption { color: #8097AA; font-size: 11px; }
QWidget#sidebar QLineEdit, QWidget#sidebar QSpinBox { background: #203747; color: #EBF1F8; border: 1px solid #365061; border-radius: 5px; min-height: 30px; max-height: 30px; padding: 0 9px; selection-background-color: #2463DC; }
QWidget#sidebar QLineEdit:focus, QWidget#sidebar QSpinBox:focus { border-color: #74A7F5; }
QWidget#sidebar QLineEdit:disabled, QWidget#sidebar QSpinBox:disabled { color: #839AAB; background: #1C303F; }
/* The IP-address group: one bordered box wrapping four octets and dots.
   Match the sidebar QLineEdit height (30px) so the field lines up with its peers. */
QWidget#sidebar IpAddressField { background: #203747; border: 1px solid #365061; border-radius: 5px; min-height: 30px; max-height: 30px; padding: 0; }
QWidget#sidebar IpAddressField[focused="true"] { border-color: #74A7F5; }
QWidget#sidebar IpAddressField:disabled { background: #1C303F; }
QWidget#sidebar QLineEdit#ipOctet { background: transparent; border: none; min-height: 22px; max-height: 22px; padding: 0; }
QWidget#sidebar QLabel#ipDot { background: transparent; color: #ACBCCB; font-size: 13px; }
/* Sidebar combos keep the light combo form; their embedded edit must not pick
   up the dark sidebar QLineEdit style (the id selector would otherwise win). */
QWidget#sidebar QComboBox QLineEdit { background: transparent; border: none; padding: 0; min-height: 0; color: #23374A; }
QWidget#sidebar QComboBox QLineEdit:disabled { color: #91A0B0; }
QWidget#sidebar QCheckBox { color: #ACBCCB; font-size: 11px; }
QWidget#sidebar QPushButton#demo { background: transparent; color: #ACBDD0; border: 1px solid #3B5264; }
QWidget#sidebar QPushButton#demo:hover { background: #233D50; color: white; }
QWidget#sidebar QPushButton#nav { background: transparent; color: #AEBDCC; border: none; border-radius: 6px; text-align: left; padding: 9px 12px; min-height: 24px; }
QWidget#sidebar QPushButton#nav:hover { background: #20394A; color: white; }
QWidget#sidebar QPushButton#nav:checked { background: #294963; color: #FFFFFF; border-left: 3px solid #72A7FF; padding-left: 9px; }
QWidget#sidebar QPushButton#nav:disabled { color: #7891A2; }
QFrame#header { background: white; border-bottom: 1px solid #DEE5EC; }
QFrame#card, QFrame#inspector { background: white; border: 1px solid #DEE5EC; border-radius: 8px; }
QFrame#addressBand { background: #F8FAFC; border: 1px solid #DEE5EC; border-radius: 7px; }
QFrame#divider { background: #DEE5EC; max-height: 1px; border: none; }
QLabel#eyebrow { font-size: 11px; color: #718397; }
QLabel#title { font-size: 23px; font-weight: 600; color: #213648; }
QLabel#sectionTitle { font-size: 15px; font-weight: 600; }
QLabel#muted { color: #718397; font-size: 11px; }
QLabel#mono, QLineEdit#mono { font-family: "Consolas"; }
QFrame#decodedFields { background: #F7F9FC; border: 1px solid #DFE7F0; border-radius: 5px; }
QLabel#fieldName { font-family: "Consolas"; font-size: 12px; color: #526D83; }
QLabel#fieldBits { font-family: "Consolas"; font-size: 10px; color: #7C91A3; }
QLabel#fieldValue { font-family: "Consolas"; font-size: 13px; font-weight: 600; }
QLabel#registerName { font-family: "Bahnschrift"; font-size: 24px; color: #23374A; }
QLabel#value { font-family: "Consolas"; font-size: 29px; color: #2463DC; font-weight: 600; }
QLabel#badge { background: #EDF2F7; color: #61778B; padding: 5px 9px; border-radius: 4px; font-size: 11px; }
QLabel#badge[state="connected"] { background: #E6F4EF; color: #157767; }
QLabel#badge[state="demo"] { background: #FFF1D9; color: #9A661D; }
QLabel#badge[state="error"] { background: #FBECEC; color: #B14040; }
QLineEdit, QSpinBox, QComboBox { background: white; border: 1px solid #D5DEE7; border-radius: 5px; min-height: 30px; padding: 0 9px; selection-background-color: #DCE9FF; selection-color: #213F73; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #2463DC; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { background: #F1F4F7; color: #91A0B0; }
QLineEdit[invalid="true"] { border-color: #C44848; background: #FFF7F7; }
QLineEdit:read-only { background: #F7F9FC; color: #5E7387; }
QComboBox { padding-right: 22px; }
/* Editable combos: the embedded QLineEdit must not draw its own box, or the
   combo looks like a field nested inside a field. Same form as the plain ones. */
QComboBox QLineEdit { background: transparent; border: none; padding: 0; min-height: 0; selection-background-color: #DCE9FF; }
QComboBox::drop-down { width: 22px; border: none; }
QComboBox::down-arrow { image: none; border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid #7B8EA0; width: 0; height: 0; }
QComboBox QAbstractItemView { background: white; selection-background-color: #E5EEFC; selection-color: #2463DC; border: 1px solid #D5DEE7; padding: 4px; }
QComboBox QAbstractItemView::item { padding: 2px 8px; min-height: 19px; }
QSpinBox::up-button, QSpinBox::down-button { width: 0; }
QPushButton { background: white; border: 1px solid #D5DEE7; border-radius: 5px; min-height: 30px; padding: 0 12px; font-weight: 500; }
QPushButton:hover { border-color: #94B4E9; background: #F1F6FF; color: #2463DC; }
QPushButton:pressed { background: #DFEAFE; }
QPushButton:focus { border-color: #2463DC; }
QPushButton:disabled { background: #F2F5F8; border-color: #E2E8EF; color: #99A7B6; }
QPushButton#primary { background: #2463DC; color: white; border: 1px solid #2463DC; }
QPushButton#primary:hover { background: #1C55C3; }
QPushButton#primary:pressed { background: #1748A7; }
QPushButton#primary:disabled { background: #A7BFEB; border-color: #A7BFEB; color: #F8FAFF; }
QPushButton#danger { color: #B64242; border-color: #E8C3C3; background: #FFF8F8; }
QPushButton#danger:hover { background: #FBE7E7; }
QPushButton#danger:pressed { background: #F5D8D8; }
QPushButton#flat { background: transparent; border: 1px solid transparent; color: #657C91; min-height: 26px; padding: 0 8px; }
QPushButton#flat:hover { background: #E9F0FA; color: #2463DC; }
QPushButton#tab { background: transparent; border: 1px solid transparent; color: #718397; min-height: 28px; padding: 0 11px; }
QPushButton#tab:checked { background: #E4EDFC; color: #245DC4; border: 1px solid #D2E1FA; }
QPushButton#rowRead { background: transparent; border: none; min-height: 26px; padding: 0 4px; color: #2463DC; }
QPushButton#rowRead:hover { background: #DDEAFE; }
QTableWidget { background: white; alternate-background-color: #F8FAFC; border: none; outline: none; gridline-color: #EFF3F7; selection-background-color: #E6EFFD; selection-color: #1F4F9B; }
QTableWidget::item { padding: 0 10px; border-bottom: 1px solid #ECF0F5; }
QTableWidget::item:selected { background: #E6EFFD; color: #204E91; }
QTableWidget::item:focus { border: 1px solid #AFC9F1; }
QTableWidget QLineEdit { min-height: 24px; padding: 0 5px; border-radius: 2px; }
/* Match the cell padding plus Fusion's 3 px text inset. */
QHeaderView::section { background: #F3F6FA; color: #6F8295; border: none; border-bottom: 1px solid #DCE4ED; padding: 10px 13px; font-size: 11px; font-weight: 500; }
QHeaderView { background: #F3F6FA; }
QScrollBar:vertical { width: 9px; background: transparent; margin: 2px; }
QScrollBar::handle:vertical { background: #CAD5E0; border-radius: 3px; min-height: 28px; }
QScrollBar:horizontal { height: 9px; background: transparent; margin: 2px; }
QScrollBar::handle:horizontal { background: #CAD5E0; border-radius: 3px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: transparent; }
QSplitter::handle:hover { background: #CADCF6; border-radius: 3px; }
QPlainTextEdit, QTextBrowser { border: none; background: transparent; }
QPlainTextEdit#console, QPlainTextEdit#boardConsole { background: #182C3B; color: #C8D7E5; font-family: "Consolas", "Microsoft YaHei UI"; font-size: 12px; border-radius: 5px; padding: 7px; selection-background-color: #365777; }
QPlainTextEdit#boardConsole { selection-background-color: #F0C478; selection-color: #142635; }
QProgressBar { border: none; border-radius: 2px; background: #E4EBF3; max-height: 3px; }
QProgressBar::chunk { background: #2463DC; border-radius: 2px; }
QProgressBar#bitProgress { max-height: 4px; }
QProgressBar#bitProgress::chunk { border-radius: 2px; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #A6B6C6; border-radius: 3px; background: white; }
QCheckBox::indicator:checked { background: #2463DC; border: 1px solid #2463DC; }
QCheckBox::indicator:disabled { background: #E5EBF2; border-color: #CAD5E0; }
/* Checked-while-disabled (fields lock during a connection): keep a muted-blue
   fill so the stored check state stays visible instead of looking cleared. */
QCheckBox::indicator:checked:disabled { background: #A7BFEB; border: 1px solid #A7BFEB; }
QStatusBar { background: white; color: #738598; border-top: 1px solid #DEE5EC; min-height: 27px; }
QStatusBar::item { border: none; }
QStatusBar QLabel { color: #738598; font-size: 11px; }
QToolTip { background: #203747; color: white; border: none; padding: 7px; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollArea#sidebarScroll QWidget#sidebar { background: #142635; }
QWidget#sidebar QTreeWidget { background: #1C303F; color: #C8D7E5; border: 1px solid #365061; border-radius: 5px; font-size: 11px; }
QWidget#sidebar QTreeWidget::item { padding: 3px 4px; border-radius: 3px; }
QWidget#sidebar QTreeWidget::item:hover { background: #233D50; }
QWidget#sidebar QTreeWidget::item:selected { background: #2F6FBF; color: #FFFFFF; }
QWidget#sidebar QTreeWidget::branch { background: transparent; }
"""
