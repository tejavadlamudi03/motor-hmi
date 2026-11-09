"""Motor Simulator window: virtual moteus controllers on a CAN-FD bus with
live view of what the HMI commands (input) and what the motor does
(output), plus fault injection."""

from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QScrollArea, QSplitter, QVBoxLayout, QWidget)

from moteus.protocol import Register as R

from .. import __version__
from ..core.faults import fault_title
from ..core.types import FirmwareMode
from ..ui import theme
from ..ui.widgets import MotorDial, button, muted
from .canfd_sim import DEFAULT_CHANNEL, DEFAULT_INTERFACE, DEFAULT_PORT, CanFdSimulator

INTERFACES = ["udp_multicast", "socketcan", "pcan", "kvaser", "vector", "ixxat", "gs_usb"]

CMD_FIELDS = [
    ("Position", R.COMMAND_POSITION, "rev"), ("Velocity", R.COMMAND_VELOCITY, "rev/s"),
    ("FF torque", R.COMMAND_FEEDFORWARD_TORQUE, "N·m"), ("Kp scale", R.COMMAND_KP_SCALE, ""),
    ("Kd scale", R.COMMAND_KD_SCALE, ""), ("Max torque", R.COMMAND_POSITION_MAX_TORQUE, "N·m"),
    ("Vel limit", R.COMMAND_VELOCITY_LIMIT, "rev/s"), ("Acc limit", R.COMMAND_ACCEL_LIMIT, "rev/s²"),
    ("Watchdog", R.COMMAND_TIMEOUT, "s"),
]


def is_virtual(interface: str, channel: str) -> bool:
    return interface == "udp_multicast" or (interface == "socketcan" and channel.startswith("vcan"))


def _f(v, unit="", d=4):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "—"
    return f"{v:+.{d}f} {unit}".strip()


class MotorCard(QGroupBox):
    def __init__(self, sim: CanFdSimulator, node_id: int, parent=None):
        super().__init__(f"Virtual moteus  ·  CAN ID {node_id}", parent)
        self.sim, self.node_id = sim, node_id
        g = QGridLayout(self)
        self.dial = MotorDial()
        self.dial.setMinimumSize(230, 260)
        g.addWidget(self.dial, 0, 0, 2, 1)

        inp = QFormLayout()
        self.req_mode = QLabel("—")
        self.req_mode.setStyleSheet("font-weight: 700;")
        inp.addRow("Requested mode", self.req_mode)
        self.cmd = {}
        for label, reg, unit in CMD_FIELDS:
            l = QLabel("—")
            l.setStyleSheet("font-family: monospace;")
            self.cmd[reg] = (l, unit)
            inp.addRow(label, l)
        self.age = QLabel("—")
        inp.addRow("Command age", self.age)
        self.frames = muted("", wrap=False)
        inp.addRow("Frames", self.frames)
        box_in = QGroupBox("From HMI (input)")
        box_in.setLayout(inp)
        g.addWidget(box_in, 0, 1)

        out = QFormLayout()
        self.o = {}
        for key in ("mode", "fault", "position", "velocity", "torque", "current", "voltage", "temp"):
            l = QLabel("—")
            l.setStyleSheet("font-family: monospace;")
            self.o[key] = l
        for key, label in (("mode", "Firmware mode"), ("fault", "Fault"), ("position", "Position (true)"),
                           ("velocity", "Velocity (true)"), ("torque", "Torque"), ("current", "Iq"),
                           ("voltage", "Supply"), ("temp", "Board temp")):
            out.addRow(label, self.o[key])
        out.addRow(muted("True, noise-free plant values – compare with the HMI's filtered feedback."))
        box_out = QGroupBox("Plant (output)")
        box_out.setLayout(out)
        g.addWidget(box_out, 0, 2)

        inj = QGridLayout()
        self.v_supply = QDoubleSpinBox(); self.v_supply.setRange(0, 60); self.v_supply.setValue(24.0); self.v_supply.setSuffix(" V")
        self.load = QDoubleSpinBox(); self.load.setRange(-3, 3); self.load.setDecimals(3); self.load.setSingleStep(0.05); self.load.setSuffix(" N·m")
        self.noise = QDoubleSpinBox(); self.noise.setRange(0, 10); self.noise.setSingleStep(0.5); self.noise.setValue(1.0); self.noise.setSuffix(" ×")
        self.cut = QCheckBox("Cable cut (no replies)")
        self.uncal = QCheckBox("Uncalibrated (fault 36)")
        inj.addWidget(QLabel("Supply voltage"), 0, 0); inj.addWidget(self.v_supply, 0, 1)
        inj.addWidget(QLabel("External load"), 0, 2); inj.addWidget(self.load, 0, 3)
        inj.addWidget(QLabel("Sensor noise"), 0, 4); inj.addWidget(self.noise, 0, 5)
        inj.addWidget(button("Overheat (85 °C)", "danger", self._overheat), 1, 0, 1, 2)
        inj.addWidget(button("Driver fault 33", "danger", lambda: self._fault(33)), 1, 2)
        inj.addWidget(button("Encoder fault 35", "danger", lambda: self._fault(35)), 1, 3)
        inj.addWidget(button("Clear injections", slot=self._clear), 1, 4, 1, 2)
        inj.addWidget(self.cut, 2, 0, 1, 2)
        inj.addWidget(self.uncal, 2, 2, 1, 2)
        box_inj = QGroupBox("Fault / disturbance injection")
        box_inj.setLayout(inj)
        g.addWidget(box_inj, 1, 1, 1, 2)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(2, 1)

        self.v_supply.valueChanged.connect(lambda v: self._set("supply_voltage", v))
        self.load.valueChanged.connect(lambda v: self._set("load_torque", v))
        self.noise.valueChanged.connect(lambda v: self._set("noise_scale", v))
        self.cut.toggled.connect(lambda on: self._set("unresponsive", on))
        self.uncal.toggled.connect(lambda on: self._set("calibrated", not on))

    def _node(self):
        return self.sim.nodes.get(self.node_id)

    def _set(self, attr, value):
        with self.sim.lock:
            n = self._node()
            if n:
                setattr(n.motor, attr, value)

    def _overheat(self):
        self._set("temp", 85.0)

    def _fault(self, code):
        self._set("injected_fault", code)

    def _clear(self):
        with self.sim.lock:
            n = self._node()
            if n:
                m = n.motor
                m.injected_fault = 0
                m.temp = 30.0
                m.load_torque = 0.0
                m.supply_voltage = 24.0
                m.noise_scale = 1.0
                m.unresponsive = False
                m.calibrated = True
        for w, v in ((self.v_supply, 24.0), (self.load, 0.0), (self.noise, 1.0)):
            w.setValue(v)
        self.cut.setChecked(False)
        self.uncal.setChecked(False)

    def refresh(self) -> None:
        now = time.monotonic()
        with self.sim.lock:
            n = self._node()
            if n is None:
                return
            m = n.motor
            last = n.last
            regs = dict(last.registers)
            mode, fault = int(m.mode), int(m.fault)
            pos, vel, tq = m.pos + m.offset, m.vel, m.torque
            temp, supply = m.temp, m.supply_voltage
            target = m.cmd.position + m.offset if math.isfinite(m.cmd.position) else math.nan
            age = now - last.time if last.time else math.nan
            frames = (n.frames_rx, n.frames_tx)
            self.setTitle(f"Virtual moteus  ·  CAN ID {n.id}")
            self.node_id = n.id
        self.req_mode.setText(FirmwareMode.name_of(last.firmware_mode) if last.time else "(no command yet – monitoring)")
        for reg, (lbl, unit) in self.cmd.items():
            lbl.setText(_f(regs.get(int(reg)), unit) if reg in regs or int(reg) in regs else "default")
        wd = regs.get(int(R.COMMAND_TIMEOUT))
        if math.isfinite(age):
            late = (wd is not None and math.isfinite(wd) and wd > 0 and age > wd
                    and mode in (FirmwareMode.POSITION, FirmwareMode.STAY_WITHIN, FirmwareMode.TIMEOUT))
            self.age.setText(f"{age * 1000:,.0f} ms" + ("  > watchdog!" if late else ""))
            self.age.setStyleSheet(f"color: {theme.ALARM if late else theme.TEXT};")
        self.frames.setText(f"RX {frames[0]:,}  TX {frames[1]:,}")
        armed = mode not in (FirmwareMode.STOPPED, FirmwareMode.FAULT)
        self.dial.set_state(pos, target, vel, tq, 2.0, 1.0, armed, mode == FirmwareMode.FAULT)
        self.o["mode"].setText(FirmwareMode.name_of(mode))
        self.o["mode"].setStyleSheet(f"color: {theme.ALARM if mode in (1, 11) else theme.TEXT};")
        self.o["fault"].setText(fault_title(fault) if fault else "none")
        self.o["position"].setText(_f(pos, "rev"))
        self.o["velocity"].setText(_f(vel, "rev/s"))
        self.o["torque"].setText(_f(tq, "N·m"))
        self.o["current"].setText(_f(tq / m.KT, "A", 2))
        self.o["voltage"].setText(f"{supply:.1f} V")
        self.o["temp"].setText(f"{temp:.1f} °C")


class SimWindow(QMainWindow):
    def __init__(self, ids=(1, 2), interface=DEFAULT_INTERFACE, channel=DEFAULT_CHANNEL,
                 prefix: int = 0, allow_hardware: bool = False, autostart: bool = True,
                 port: int = DEFAULT_PORT):
        super().__init__()
        self.setWindowTitle(f"Motor Simulator {__version__} – virtual moteus on CAN-FD")
        self.resize(1250, 900)
        self.allow_hardware = allow_hardware
        self.sim: CanFdSimulator | None = None
        self.cards: list[MotorCard] = []

        top = QWidget()
        top.setObjectName("top")
        top.setStyleSheet(f"#top {{ background: {theme.PANEL}; border-bottom: 1px solid {theme.BORDER}; }}")
        h = QHBoxLayout(top)
        t = QLabel("Motor Simulator")
        t.setStyleSheet("font-size: 14pt; font-weight: 700;")
        h.addWidget(t)
        b = QLabel("SIMULATED CONTROLLERS")
        b.setProperty("role", "banner")
        h.addWidget(b)
        h.addSpacing(16)
        self.iface = QComboBox()
        self.iface.addItems(INTERFACES)
        self.iface.setCurrentText(interface)
        self.chan = QLineEdit(channel)
        self.chan.setMinimumWidth(130)
        self.port = QLineEdit(str(port))
        self.port.setMinimumWidth(70)
        self.port.setMaximumWidth(90)
        self.ids = QLineEdit(",".join(str(i) for i in ids))
        self.ids.setMaximumWidth(90)
        self.prefix_edit = QLineEdit(str(prefix))
        self.prefix_edit.setMaximumWidth(50)
        self.prefix_edit.setToolTip("moteus can.prefix (normally 0). Must match 'CAN prefix' in the HMI.")
        for lbl, w in (("Bus", self.iface), ("Channel", self.chan), ("Port", self.port),
                       ("Prefix", self.prefix_edit), ("CAN IDs", self.ids)):
            h.addWidget(QLabel(lbl))
            h.addWidget(w)
        self.start_btn = button("Start", "primary", self._toggle)
        h.addWidget(self.start_btn)
        h.addStretch(1)
        self.stats = QLabel("stopped")
        h.addWidget(self.stats)

        self.cards_host = QWidget()
        self.cards_lay = QVBoxLayout(self.cards_host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.cards_host)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setStyleSheet("font-family: monospace; font-size: 8pt;")
        self.show_log = QCheckBox("Show frame log")
        split = QSplitter(Qt.Vertical)
        split.addWidget(scroll)
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(6, 0, 6, 6)
        ll.addWidget(self.show_log)
        ll.addWidget(self.log)
        split.addWidget(lw)
        split.setSizes([760, 140])
        help_ = muted("In Motor HMI choose Controller = mjbots moteus …, Interface = Virtual CAN-FD bus "
                      "(same group and port as here) and Connect. The HMI then uses the same moteus "
                      "library / python-can driver path as with a real USB-CAN-FD adapter.")
        self.waiting = QLabel("")
        self.waiting.setWordWrap(True)
        self.waiting.setStyleSheet(f"background: {theme.WARN}; color: #1b1b1b; font-weight: 700; "
                                   "padding: 8px 12px; font-size: 11pt;")
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(top)
        cl.addWidget(self.waiting)
        cl.addWidget(help_)
        cl.addWidget(split, 1)
        self.setCentralWidget(central)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self.timer.start(66)
        self._last = (time.monotonic(), 0, 0)
        if autostart:
            QTimer.singleShot(0, self._toggle)

    def _parse_ids(self) -> list[int]:
        out = []
        for part in self.ids.text().replace(";", ",").split(","):
            if part.strip().isdigit() and 1 <= int(part) <= 126:
                out.append(int(part))
        return sorted(set(out)) or [1]

    def _toggle(self) -> None:
        if self.sim:
            self.sim.stop()
            self.sim = None
            for c in self.cards:
                c.deleteLater()
            self.cards = []
            self.start_btn.setText("Start")
            self.stats.setText("stopped")
            for w in (self.iface, self.chan, self.port, self.prefix_edit, self.ids):
                w.setEnabled(True)
            return
        iface, chan = self.iface.currentText(), self.chan.text().strip()
        if not is_virtual(iface, chan) and not self.allow_hardware:
            QMessageBox.warning(self, "Real bus refused",
                                f"'{iface}:{chan}' looks like a physical CAN bus. A simulated controller "
                                "on a real machine's bus could spoof feedback. Restart with "
                                "--allow-hardware-bus for hardware-in-the-loop benches.")
            return
        try:
            extra = {"port": int(self.port.text() or DEFAULT_PORT)} if iface == "udp_multicast" else {}
            prefix = int(self.prefix_edit.text() or 0) if self.prefix_edit.text().strip().isdigit() else 0
            self.sim = CanFdSimulator(ids=self._parse_ids(), interface=iface, channel=chan,
                                      can_prefix=prefix, **extra)
            self.sim.start()
        except Exception as e:
            self.sim = None
            QMessageBox.critical(self, "Cannot open bus", f"{iface}:{chan}\n\n{e}")
            return
        for nid in self.sim.nodes:
            card = MotorCard(self.sim, nid)
            self.cards.append(card)
            self.cards_lay.addWidget(card)
        self.start_btn.setText("Stop")
        for w in (self.iface, self.chan, self.port, self.prefix_edit, self.ids):
            w.setEnabled(False)

    def _refresh(self) -> None:
        if not self.sim:
            return
        for c in self.cards:
            c.refresh()
        s = self.sim.stats
        now = time.monotonic()
        t0, rx0, tx0 = self._last
        dt = max(1e-3, now - t0)
        self.stats.setText(f"{self.sim.interface}:{self.sim.channel}   RX {s['rx']:,} ({(s['rx'] - rx0) / dt:,.0f}/s)"
                           f"   TX {s['tx']:,} ({(s['tx'] - tx0) / dt:,.0f}/s)   errors {s['errors']}")
        self._last = (now, s["rx"], s["tx"])
        quiet = (s["rx"] - rx0) == 0
        self._quiet_since = getattr(self, "_quiet_since", now) if quiet else now
        ign_p, ign_i = s.get("ignored_prefix", 0), s.get("ignored_id", 0)
        d_p = ign_p - getattr(self, "_ign_p0", 0)
        d_i = ign_i - getattr(self, "_ign_i0", 0)
        self._ign_p0, self._ign_i0 = ign_p, ign_i
        if d_p > 0:
            self._prefix_issue_t = now
        if d_i > 0:
            self._id_issue_t = now
        d_p = now - getattr(self, "_prefix_issue_t", -1e9) < 2.0      # recent mismatch?
        d_i = now - getattr(self, "_id_issue_t", -1e9) < 2.0
        if d_p:
            self.waiting.setText(
                f"The HMI is sending, but with CAN prefix {self.sim.last_ignored_prefix} – these simulated "
                f"controllers use prefix {self.sim.prefix}, so they ignore it. In the HMI: Disconnect, set "
                f"'CAN prefix' = {self.sim.prefix} (it is NOT the motor ID), Connect.")
            self.waiting.setVisible(True)
        elif d_i and quiet:
            self.waiting.setText(
                f"The HMI is addressing CAN ID {self.sim.last_ignored_id}, but this simulator has IDs "
                f"{sorted(self.sim.nodes)}. In the HMI clear 'Node IDs' (auto-discover) or use these IDs.")
            self.waiting.setVisible(True)
        elif quiet and now - self._quiet_since > 1.5:
            port = self.port.text()
            self.waiting.setText(
                "Waiting for the HMI – no CAN-FD frames received. In Motor HMI select "
                "Controller = mjbots moteus …, Interface = \"Motor Simulator app (virtual CAN-FD bus)\", "
                f"Bus group {self.sim.channel}, Bus port {port}, then Connect. "
                "(\"Built-in simulation\" runs inside the HMI and does not use this app – Disconnect, "
                "change Interface, Connect.)")
            self.waiting.setVisible(True)
        else:
            self.waiting.setVisible(False)
        if self.show_log.isChecked():
            lines = []
            while self.sim.frame_log:
                lines.append(self.sim.frame_log.popleft())
            if lines:
                self.log.appendPlainText("\n".join(lines[-200:]))
        else:
            self.sim.frame_log.clear()

    def closeEvent(self, ev) -> None:
        if self.sim:
            self.sim.stop()
        ev.accept()
