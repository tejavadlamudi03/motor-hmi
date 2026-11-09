"""Reusable widgets."""

from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSlider, QVBoxLayout, QWidget)

from . import theme

pg.setConfigOptions(antialias=False, background=theme.PLOT_BG, foreground=theme.MUTED)

UNIT_FACTORS = {"rev": 1.0, "rad": 2.0 * math.pi, "deg": 360.0}


def unit_label(base: str, units: str) -> str:
    if base == "rev":
        return units
    if base == "rev/s":
        return f"{units}/s"
    if base == "rev/s²":
        return f"{units}/s²"
    return base


def to_display(value: float, base: str, units: str) -> float:
    return value * UNIT_FACTORS[units] if base.startswith("rev") else value


def from_display(value: float, base: str, units: str) -> float:
    return value / UNIT_FACTORS[units] if base.startswith("rev") else value


def fmt(v: float, digits: int = 4) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    return f"{v:+.{digits}f}" if isinstance(v, float) else str(v)


class Led(QWidget):
    def __init__(self, color: str = theme.MUTED, size: int = 14, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor("#00000000"))
        p.setBrush(self._color)
        p.drawEllipse(1, 1, self.width() - 2, self.height() - 2)


class ValueTile(QFrame):
    """Label + large numeric readout."""

    def __init__(self, title: str, unit: str = "", parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"QFrame {{ background: {theme.PANEL}; border: 1px solid {theme.BORDER}; border-radius: 6px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(0)
        self.title = QLabel(title)
        self.title.setProperty("role", "muted")
        self.value = QLabel("—")
        self.value.setProperty("role", "value")
        self.unit = QLabel(unit)
        self.unit.setProperty("role", "muted")
        row = QHBoxLayout()
        row.addWidget(self.value)
        row.addStretch(1)
        row.addWidget(self.unit, 0, Qt.AlignBottom)
        lay.addWidget(self.title)
        lay.addLayout(row)

    def set(self, text: str, color: Optional[str] = None, unit: Optional[str] = None) -> None:
        self.value.setText(text)
        self.value.setStyleSheet(f"color: {color};" if color else "")
        if unit is not None:
            self.unit.setText(unit)


class HoldButton(QPushButton):
    """Hold-to-run button (jog).  Emits ``held(True)`` on press and
    ``held(False)`` on release, focus loss or when hidden - so the motor
    never keeps moving because a release event was missed."""

    held = Signal(bool)

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._down = False
        self.setAutoRepeat(False)

    def _set(self, down: bool) -> None:
        if down != self._down:
            self._down = down
            self.held.emit(down)

    def mousePressEvent(self, e):
        super().mousePressEvent(e)
        if self.isEnabled():
            self._set(True)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        self._set(False)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self._set(False)

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._set(False)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._set(False)

    def setEnabled(self, on: bool) -> None:
        if not on:
            self._set(False)
        super().setEnabled(on)


class ParamSlider(QWidget):
    """Numeric entry + slider + MIN/MAX labels, kept in sync.  The range
    is always bounded by the active safety limits."""

    valueChanged = Signal(float)
    STEPS = 1000

    def __init__(self, name: str, unit: str, lo: float, hi: float, value: float = 0.0,
                 decimals: int = 3, parent=None):
        super().__init__(parent)
        self._lo, self._hi = lo, hi
        self._block = False
        g = QGridLayout(self)
        g.setContentsMargins(0, 2, 0, 2)
        g.setHorizontalSpacing(6)
        g.setVerticalSpacing(0)
        self.name = QLabel(name)
        self.name.setMinimumWidth(70)
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(decimals)
        self.spin.setKeyboardTracking(False)
        self.spin.setMinimumWidth(110)
        self.unit = QLabel(unit)
        self.unit.setProperty("role", "muted")
        self.unit.setMinimumWidth(48)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, self.STEPS)
        self.min_lbl = QLabel()
        self.max_lbl = QLabel()
        for l in (self.min_lbl, self.max_lbl):
            l.setProperty("role", "muted")
            l.setStyleSheet("font-size: 8pt;")
        g.addWidget(self.name, 0, 0)
        g.addWidget(self.spin, 0, 1, 1, 2)
        g.addWidget(self.unit, 0, 3)
        g.addWidget(self.min_lbl, 1, 0, Qt.AlignRight)
        g.addWidget(self.slider, 1, 1, 1, 2)
        g.addWidget(self.max_lbl, 1, 3)
        self.spin.valueChanged.connect(self._from_spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.set_range(lo, hi)
        self.set_value(value)

    def set_range(self, lo: float, hi: float, unit: Optional[str] = None) -> None:
        self._lo, self._hi = lo, hi
        self._block = True
        self.spin.setRange(lo, hi)
        self._block = False
        d = self.spin.decimals()
        self.min_lbl.setText(f"MIN {lo:.{min(d, 2)}f}")
        self.max_lbl.setText(f"MAX {hi:.{min(d, 2)}f}")
        if unit is not None:
            self.unit.setText(unit)
        self._sync_slider()

    def value(self) -> float:
        return float(self.spin.value())

    def set_value(self, v: float) -> None:
        if not math.isfinite(v):
            v = 0.0
        self.spin.setValue(max(self._lo, min(self._hi, v)))
        self._sync_slider()

    def _sync_slider(self) -> None:
        span = self._hi - self._lo
        pos = 0 if span <= 0 else int(round((self.spin.value() - self._lo) / span * self.STEPS))
        self._block = True
        self.slider.setValue(pos)
        self._block = False

    def _from_spin(self, v: float) -> None:
        if self._block:
            return
        self._sync_slider()
        self.valueChanged.emit(float(v))

    def _from_slider(self, pos: int) -> None:
        if self._block:
            return
        v = self._lo + (self._hi - self._lo) * pos / self.STEPS
        self._block = True
        self.spin.setValue(v)
        self._block = False
        self.valueChanged.emit(float(self.spin.value()))


class LivePlot(QWidget):
    """Scrolling time plot with a header showing the latest value."""

    def __init__(self, title: str, unit: str, color: str, capacity: int = 4000, parent=None):
        super().__init__(parent)
        self.title_text, self.unit_text, self.color = title, unit, color
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.header = QLabel()
        self.header.setAlignment(Qt.AlignCenter)
        self.header.setStyleSheet(f"color: {color}; font-weight: 700; font-family: monospace;")
        lay.addWidget(self.header)
        self.pw = pg.PlotWidget()
        self.pw.showGrid(x=True, y=True, alpha=0.15)
        self.pw.setMouseEnabled(x=False, y=True)
        self.pw.setLabel("bottom", "time", units="s")
        lay.addWidget(self.pw, 1)
        self.cap = capacity
        self.t = np.full(capacity, np.nan)
        self.series: dict[str, np.ndarray] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.n = 0
        self.window_s = 10.0
        self.add_series("main", color, 2)

    def add_series(self, key: str, color: str, width: int = 1, dash: bool = False) -> None:
        pen = pg.mkPen(color, width=width, style=Qt.DashLine if dash else Qt.SolidLine)
        self.series[key] = np.full(self.cap, np.nan)
        self.curves[key] = self.pw.plot(pen=pen)

    def set_unit(self, unit: str) -> None:
        self.unit_text = unit

    def clear(self) -> None:
        self.n = 0
        self.t[:] = np.nan
        for a in self.series.values():
            a[:] = np.nan

    def append(self, t: float, values: dict[str, float]) -> None:
        i = self.n % self.cap
        self.t[i] = t
        for k, a in self.series.items():
            a[i] = values.get(k, np.nan)
        self.n += 1

    def set_visible_series(self, key: str, visible: bool) -> None:
        if key in self.curves:
            self.curves[key].setVisible(visible)

    def redraw(self, latest: Optional[float]) -> None:
        self.header.setText(f"{self.title_text}: {fmt(latest, 5)}  {self.unit_text}")
        if self.n == 0:
            for c in self.curves.values():
                c.setData([], [])
            return
        count = min(self.n, self.cap)
        start = self.n - count
        idx = (np.arange(start, self.n) % self.cap)
        t = self.t[idx]
        tmax = t[-1]
        mask = t >= tmax - self.window_s
        tt = t[mask] - tmax
        for k, c in self.curves.items():
            if c.isVisible():
                c.setData(tt, self.series[k][idx][mask], connect="finite")
        self.pw.setXRange(-self.window_s, 0, padding=0)


def confirm(parent, title: str, text: str, typed: Optional[str] = None, danger: bool = True) -> bool:
    """Confirmation dialog; when ``typed`` is given the user must type it."""
    if typed is None:
        r = QMessageBox.warning(parent, title, text, QMessageBox.Yes | QMessageBox.Cancel,
                                QMessageBox.Cancel)
        return r == QMessageBox.Yes
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    lbl = QLabel(text + f"\n\nType  {typed}  to confirm.")
    lbl.setWordWrap(True)
    lay.addWidget(lbl)
    edit = QLineEdit()
    lay.addWidget(edit)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    ok = bb.button(QDialogButtonBox.Ok)
    ok.setEnabled(False)
    ok.setProperty("kind", "danger" if danger else "primary")
    edit.textChanged.connect(lambda s: ok.setEnabled(s.strip() == typed))
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.resize(460, 200)
    return dlg.exec() == QDialog.Accepted


def group(title: str, layout) -> QGroupBox:
    g = QGroupBox(title)
    g.setLayout(layout)
    return g


def muted(text: str, wrap: bool = True) -> QLabel:
    l = QLabel(text)
    l.setProperty("role", "muted")
    l.setWordWrap(wrap)
    return l


def button(text: str, kind: str = "", slot: Optional[Callable] = None, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    if slot:
        b.clicked.connect(slot)
    if tip:
        b.setToolTip(tip)
    return b


class MotorDial(QWidget):
    """Live picture of the motor: rotor with magnet poles turning at the
    measured shaft angle, setpoint marker, speed arc and torque bar."""

    def __init__(self, poles: int = 14, parent=None):
        super().__init__(parent)
        self.poles = poles
        self.pos = math.nan
        self.target = math.nan
        self.vel = 0.0
        self.torque = 0.0
        self.max_vel = 1.0
        self.max_torque = 1.0
        self.armed = False
        self.fault = False
        self.setMinimumSize(QSize(220, 250))

    def set_state(self, pos, target, vel, torque, max_vel, max_torque, armed, fault) -> None:
        self.pos, self.target = pos, target
        self.vel = vel if math.isfinite(vel) else 0.0
        self.torque = torque if math.isfinite(torque) else 0.0
        self.max_vel, self.max_torque = max(max_vel, 1e-6), max(max_torque, 1e-6)
        self.armed, self.fault = armed, fault
        self.update()

    def paintEvent(self, _):
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QBrush, QFont, QPen, QPolygonF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        size = min(w, h - 40)
        cx, cy, r = w / 2, (h - 40) / 2 + 4, size / 2 - 14

        def pt(angle_deg, radius):
            a = math.radians(angle_deg - 90)
            return QPointF(cx + radius * math.cos(a), cy + radius * math.sin(a))

        # stator ring + ticks
        p.setPen(QPen(QColor(theme.BORDER), 10))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(QColor(theme.MUTED), 1))
        for k in range(12):
            p.drawLine(pt(k * 30, r - 4), pt(k * 30, r + 4))
        # speed arc (direction = sign)
        frac = max(-1.0, min(1.0, self.vel / self.max_vel))
        if abs(frac) > 0.01:
            p.setPen(QPen(QColor(theme.ALARM if abs(frac) >= 0.999 else theme.ACCENT), 5))
            rect = QRectF(cx - r - 9, cy - r - 9, 2 * r + 18, 2 * r + 18)
            p.drawArc(rect, 90 * 16, int(-frac * 360 * 16))
        known = math.isfinite(self.pos)
        ang = (self.pos % 1.0) * 360.0 if known else 0.0
        # rotor with alternating magnet poles
        rr = r - 14
        n = max(2, self.poles)
        for k in range(n):
            col = QColor("#7a2e33" if k % 2 == 0 else "#2b4a7a")
            if not known:
                col = QColor(theme.PANEL_2)
            p.setPen(QPen(QColor(theme.BG), 1))
            p.setBrush(QBrush(col))
            start = -(ang + k * 360.0 / n) + 90
            p.drawPie(QRectF(cx - rr, cy - rr, 2 * rr, 2 * rr), int(start * 16), int(-360.0 / n * 16))
        p.setBrush(QBrush(QColor(theme.PANEL)))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawEllipse(QPointF(cx, cy), rr * 0.55, rr * 0.55)
        # target marker
        if math.isfinite(self.target):
            ta = (self.target % 1.0) * 360.0
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(theme.WARN)))
            p.drawPolygon(QPolygonF([pt(ta, r + 2), pt(ta - 5, r + 14), pt(ta + 5, r + 14)]))
        # shaft needle
        if known:
            color = theme.ALARM if self.fault else (theme.TEXT if not self.armed else "#ffffff")
            p.setPen(QPen(QColor(color), 4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(pt(ang, rr * 0.58), pt(ang, rr * 0.97))   # keep the hub free for text
            p.setBrush(QBrush(QColor(color)))
            p.drawEllipse(pt(ang, rr * 0.97), 4, 4)
        # centre text
        p.setPen(QColor(theme.TEXT))
        f = QFont(p.font())
        f.setPointSizeF(9)
        f.setBold(True)
        p.setFont(f)
        txt = f"{ang:5.1f}°\nturn {math.floor(self.pos):+d}" if known else "no position"
        p.drawText(QRectF(cx - rr * 0.5, cy - 18, rr, 36), Qt.AlignCenter, txt)
        # torque bar
        y = h - 26
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(QBrush(QColor("#171a1f")))
        p.drawRect(QRectF(10, y, w - 20, 10))
        tf = max(-1.0, min(1.0, self.torque / self.max_torque))
        mid = w / 2
        p.setBrush(QBrush(QColor(theme.ALARM if abs(tf) >= 0.999 else theme.TRACE["torque"])))
        p.setPen(Qt.NoPen)
        p.drawRect(QRectF(mid, y + 1, tf * (w / 2 - 11), 8))
        p.setPen(QColor(theme.MUTED))
        f.setBold(False)
        f.setPointSizeF(8)
        p.setFont(f)
        p.drawText(QRectF(10, y + 11, w - 20, 14), Qt.AlignCenter,
                   f"torque {self.torque:+.3f} N·m   speed {self.vel:+.3f} rev/s")
