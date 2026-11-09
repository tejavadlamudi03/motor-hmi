"""Visual theme.

Follows ISA-101 high-performance HMI guidance: neutral grey surfaces,
colour reserved for state that needs attention (amber = warning,
red = alarm / trip / E-stop, blue = energised / armed).  Plots use a
dark canvas so traces stay readable on shop-floor displays.
"""

from __future__ import annotations

BG = "#1f2328"
PANEL = "#272c33"
PANEL_2 = "#2f353d"
BORDER = "#3c434d"
TEXT = "#e6e8eb"
MUTED = "#9aa3ad"
ACCENT = "#4c8dff"     # armed / energised / primary action
OK = "#3fb68b"         # healthy communication only
WARN = "#e8a33d"
ALARM = "#e5484d"
PLOT_BG = "#111418"

TRACE = {
    "raw": "#5b6672",
    "filtered": "#4c8dff",
    "command": "#e8a33d",
    "position": "#ff6b6b",
    "velocity": "#4c8dff",
    "torque": "#3fb68b",
}

QSS = f"""
* {{ font-size: 10pt; }}
QWidget {{ background: {BG}; color: {TEXT}; }}
QMainWindow::separator {{ background: {BORDER}; width: 1px; }}
QGroupBox {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px;
            margin-top: 14px; padding: 8px 8px 6px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED};
                   font-weight: 600; }}
QLabel {{ background: transparent; }}
QLabel[role="muted"] {{ color: {MUTED}; }}
QLabel[role="value"] {{ font-size: 15pt; font-weight: 600; font-family: "DejaVu Sans Mono", Consolas, monospace; }}
QLabel[role="banner"] {{ background: {WARN}; color: #1b1b1b; font-weight: 700; padding: 3px 10px; border-radius: 4px; }}
QLabel[role="alarmbanner"] {{ background: {ALARM}; color: white; font-weight: 700; padding: 3px 10px; border-radius: 4px; }}
QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 5px; padding: 5px 12px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: #5c6670; border-color: #30363d; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton[kind="danger"] {{ background: #5a2326; border-color: {ALARM}; color: #ffd7d8; }}
QPushButton[kind="estop"] {{ background: {ALARM}; border: 3px solid #ffb3b5; color: white; font-weight: 800;
                             font-size: 13pt; border-radius: 8px; padding: 8px 18px; }}
QPushButton[kind="estop"]:pressed {{ background: #b3262b; }}
QPushButton:checked {{ background: {ACCENT}; color: white; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: #171a1f; border: 1px solid {BORDER}; border-radius: 4px; padding: 3px 6px;
    selection-background-color: {ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {PANEL}; selection-background-color: {ACCENT}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; top: -1px; background: {BG}; }}
QTabBar::tab {{ background: {PANEL}; border: 1px solid {BORDER}; border-bottom: none; padding: 6px 12px;
               border-top-left-radius: 5px; border-top-right-radius: 5px; margin-right: 2px; color: {MUTED}; }}
QTabBar::tab:selected {{ background: {BG}; color: {TEXT}; border-color: {ACCENT}; }}
QTableWidget, QTableView, QTreeWidget, QListWidget {{ background: #171a1f; gridline-color: {BORDER};
    border: 1px solid {BORDER}; alternate-background-color: #1b1f24; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-right: 1px solid {BORDER};
                       padding: 4px 6px; font-weight: 600; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; }}
QToolBar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; spacing: 8px; padding: 4px; }}
QScrollArea {{ border: none; }}
QSlider::groove:horizontal {{ height: 6px; background: {BORDER}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 16px; margin: -6px 0; border-radius: 8px; }}
QSlider::sub-page:horizontal {{ background: #35507f; border-radius: 3px; }}
QProgressBar {{ border: 1px solid {BORDER}; border-radius: 4px; text-align: center; background: #171a1f; }}
QProgressBar::chunk {{ background: {ACCENT}; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {MUTED}; border-radius: 3px;
                        background: #171a1f; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:disabled {{ border-color: #3c434d; }}
QPushButton[kind="primary"]:disabled, QPushButton[kind="danger"]:disabled {{
    background: {PANEL_2}; border-color: #30363d; color: #5c6670; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; }}
"""
