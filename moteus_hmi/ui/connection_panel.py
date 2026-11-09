"""Left panel: controller selection, link / port settings, connect."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QSpinBox,
                               QStackedWidget, QVBoxLayout, QWidget)

from .. import __version__
from ..core.controllers import PROFILES, get_profile
from ..core.moteus_backend import list_serial_ports
from ..core.types import TRANSPORT_KINDS, TransportConfig
from ..security.auth import Perm
from . import theme
from .widgets import Led, button, group, muted

PYTHONCAN_IFACES = ["pcan", "kvaser", "vector", "ixxat", "gs_usb", "socketcan", "neovi", "nican", "systec"]
BAUDS = ["115200", "230400", "460800", "921600", "1843200"]


class ConnectionPanel(QWidget):
    connect_requested = Signal(str, object)     # profile key, TransportConfig
    disconnect_requested = Signal()
    goto_tab = Signal(str)

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setMinimumWidth(270)
        self.setMaximumWidth(330)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        f = QFormLayout()
        self.profile = QComboBox()
        for p in PROFILES.values():
            self.profile.addItem(p.title, p.key)
        self.profile.setToolTip("Controller family / driver")
        self.link = QComboBox()
        f.addRow("Controller", self.profile)
        f.addRow("Interface", self.link)
        self.note = muted("")
        self.note.setStyleSheet("font-size: 8pt;")
        f.addRow(self.note)

        self.stack = QStackedWidget()
        self.pages: dict[str, int] = {}
        # serial (fdcanusb / ST)
        ser = QWidget()
        sf = QFormLayout(ser)
        sf.setContentsMargins(0, 0, 0, 0)
        self.port = QComboBox()
        self.port.setEditable(True)
        self.baud = QComboBox(); self.baud.addItems(BAUDS); self.baud.setEditable(True)
        self.databits = QComboBox(); self.databits.addItems(["8"])
        self.parity = QComboBox(); self.parity.addItems(["None"])
        self.stopbits = QComboBox(); self.stopbits.addItems(["One"])
        self.apd = QDoubleSpinBox(); self.apd.setDecimals(7); self.apd.setRange(0, 1); self.apd.setSingleStep(0.0001)
        self.apd.setToolTip("Power-board current scaling: amps per ADC digit (0 = profile default)")
        self.kt = QDoubleSpinBox(); self.kt.setDecimals(4); self.kt.setRange(0, 10)
        self.kt.setToolTip("Motor torque constant N·m/A (0 = profile default)")
        sf.addRow("Serial port", self.port)
        sf.addRow("Baud rate", self.baud)
        sf.addRow("Data bits", self.databits)
        sf.addRow("Parity", self.parity)
        sf.addRow("Stop bits", self.stopbits)
        self.st_rows = [(QLabel("A / digit"), self.apd), (QLabel("Kt N·m/A"), self.kt)]
        for l, w in self.st_rows:
            sf.addRow(l, w)
        self._add_page("serial", ser)
        # socketcan
        sc = QWidget()
        scf = QFormLayout(sc)
        scf.setContentsMargins(0, 0, 0, 0)
        self.sc_chan = QLineEdit("can0")
        scf.addRow("Channel", self.sc_chan)
        scf.addRow(muted("Bring up with: ip link set can0 up type can bitrate 1000000 dbitrate 5000000 "
                         "sample-point 0.666 dsample-point 0.666 fd on"))
        self._add_page("socketcan", sc)
        # python-can
        pc = QWidget()
        pcf = QFormLayout(pc)
        pcf.setContentsMargins(0, 0, 0, 0)
        self.pc_iface = QComboBox(); self.pc_iface.addItems(PYTHONCAN_IFACES); self.pc_iface.setEditable(True)
        self.pc_chan = QLineEdit("PCAN_USBBUS1")
        pcf.addRow("Interface", self.pc_iface)
        pcf.addRow("Channel", self.pc_chan)
        self._add_page("pythoncan", pc)
        # virtual CAN-FD (Motor Simulator app)
        vc = QWidget()
        vcf = QFormLayout(vc)
        vcf.setContentsMargins(0, 0, 0, 0)
        self.v_chan = QLineEdit("239.74.163.2")
        self.v_chan.setToolTip("UDP multicast group used as virtual CAN-FD bus")
        self.v_ids = QLineEdit("1,2")
        self.v_port = QSpinBox(); self.v_port.setRange(1024, 65535); self.v_port.setValue(43113)
        vcf.addRow("Bus group", self.v_chan)
        vcf.addRow("Bus port", self.v_port)
        vcf.addRow("Sim IDs", self.v_ids)
        vcf.addRow(button("▶ Launch Motor Simulator", slot=self._launch_sim,
                          tip="Start the simulator app with virtual moteus controllers"))
        hint = muted("Start the simulator, then Connect.")
        hint.setToolTip("Uses the real moteus driver path (moteus library + python-can) "
                        "against simulated controllers on a virtual CAN-FD bus.")
        vcf.addRow(hint)
        self._add_page("virtual_canfd", vc)
        # auto
        au = QWidget()
        QVBoxLayout(au).addWidget(muted("Uses every fdcanusb found, otherwise any CAN-FD adapter "
                                        "python-can can enumerate."))
        self._add_page("auto", au)
        f.addRow(self.stack)

        self.can_opts = QWidget()
        cf = QFormLayout(self.can_opts)
        cf.setContentsMargins(0, 0, 0, 0)
        self.prefix = QSpinBox(); self.prefix.setRange(0, 8191)
        self.prefix.setToolTip("Advanced: moteus 'can.prefix' shared by all controllers on the bus. "
                               "Leave 0 unless your controllers were configured with a prefix. "
                               "This is NOT the motor ID.")
        self.ids = QLineEdit(); self.ids.setPlaceholderText("blank = auto-discover")
        self.ids.setToolTip("Leave blank to find every controller automatically, or list IDs, e.g. 1,2")
        self.brs = QCheckBox("Disable bit-rate switch (BRS)")
        cf.addRow("CAN prefix (adv.)", self.prefix)
        cf.addRow("Node IDs", self.ids)
        cf.addRow(self.brs)
        f.addRow(self.can_opts)

        lay.addWidget(group("Connection", f))
        row = QHBoxLayout()
        self.refresh_btn = button("⟳ Refresh", slot=self._refresh_ports)
        self.connect_btn = button("Connect", "primary", self._toggle)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.connect_btn, 1)
        lay.addLayout(row)
        st = QHBoxLayout()
        self.led = Led(theme.MUTED)
        self.state = QLabel("Disconnected")
        self.state.setWordWrap(True)
        st.addWidget(self.led)
        st.addWidget(self.state, 1)
        lay.addLayout(st)

        ml = QVBoxLayout()
        self.motors = QListWidget()
        self.motors.setMaximumHeight(150)
        self.motors.currentItemChanged.connect(self._motor_clicked)
        ml.addWidget(self.motors)
        lay.addWidget(group("Motors", ml))

        q = QVBoxLayout()
        r1 = QHBoxLayout()
        r1.addWidget(button("Export params", slot=lambda: self.goto_tab.emit("export")))
        r1.addWidget(button("Import params", slot=lambda: self.goto_tab.emit("import")))
        q.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(button("Calibrate", slot=lambda: self.goto_tab.emit("calibration")))
        r2.addWidget(button("Firmware", slot=lambda: self.goto_tab.emit("firmware")))
        q.addLayout(r2)
        lay.addWidget(group("Quick actions", q))
        lay.addStretch(1)
        ver = muted(f"Version: {__version__}", wrap=False)
        lay.addWidget(ver)

        self.profile.currentIndexChanged.connect(self._profile_changed)
        self.link.currentIndexChanged.connect(self._link_changed)
        self._load(ctx.settings.transport_config())
        self._refresh_ports()

    def _add_page(self, key: str, w: QWidget) -> None:
        self.pages[key] = self.stack.addWidget(w)

    def _show_page(self, index: int) -> None:
        # Size the stack to the visible page only (no empty gap).
        from PySide6.QtWidgets import QSizePolicy
        for i in range(self.stack.count()):
            pol = QSizePolicy.Preferred if i == index else QSizePolicy.Ignored
            self.stack.widget(i).setSizePolicy(pol, pol)
        self.stack.setCurrentIndex(index)
        self.stack.adjustSize()

    # ------------------------------------------------------------------
    def _load(self, cfg: TransportConfig) -> None:
        i = self.profile.findData(cfg.profile)
        self.profile.setCurrentIndex(max(0, i))
        self._profile_changed()
        j = self.link.findData(cfg.kind)
        if j >= 0:
            self.link.setCurrentIndex(j)
        self.port.setEditText(cfg.serial_port or cfg.fdcanusb_path)
        self.baud.setCurrentText(str(cfg.baudrate))
        self.sc_chan.setText(cfg.can_channel if cfg.kind == "socketcan" else "can0")
        self.pc_iface.setCurrentText(cfg.can_interface)
        if cfg.kind == "pythoncan":
            self.pc_chan.setText(cfg.can_channel)
        self.prefix.setValue(cfg.can_prefix)
        self.ids.setText(",".join(str(i) for i in cfg.explicit_ids))
        self.brs.setChecked(cfg.disable_brs)
        self.v_chan.setText(cfg.virtual_channel)
        self.v_port.setValue(int(cfg.virtual_port))
        self.apd.setValue(cfg.amps_per_digit)
        self.kt.setValue(cfg.torque_constant)

    def _profile_changed(self, *_):
        prof = get_profile(self.profile.currentData())
        cur = self.link.currentData()
        self.link.blockSignals(True)
        self.link.clear()
        for k in prof.links:
            self.link.addItem(TRANSPORT_KINDS[k], k)
        j = self.link.findData(cur)
        self.link.setCurrentIndex(j if j >= 0 else 0)
        self.link.blockSignals(False)
        self.note.setText(f"{prof.min_voltage:g}–{prof.max_voltage:g} V · {prof.peak_current_A:g} A peak · "
                          f"{', '.join(m.label for m in prof.modes[:4])}  ⓘ")
        self.note.setToolTip(prof.notes)
        self.profile.setToolTip(prof.notes)
        if prof.driver == "st_mcsdk":
            self.apd.setValue(self.apd.value() or prof.params.get("amps_per_digit", 0))
            self.kt.setValue(self.kt.value() or prof.params.get("torque_constant_Nm_per_A", 0))
            self.baud.setCurrentText(str(prof.params.get("baudrate", 115200)))
        self._link_changed()

    def _link_changed(self, *_):
        kind = self.link.currentData() or "auto"
        prof = get_profile(self.profile.currentData())
        page = "serial" if kind in ("fdcanusb", "st_serial") else kind
        self._show_page(self.pages.get(page, self.pages["auto"]))
        st = kind == "st_serial"
        for l, w in self.st_rows:
            l.setVisible(st)
            w.setVisible(st)
        for w in (self.baud, self.databits, self.parity, self.stopbits):
            w.setEnabled(st)
        self.can_opts.setVisible(prof.driver == "moteus")

    def _refresh_ports(self) -> None:
        cur = self.port.currentText()
        self.port.clear()
        for dev, desc in list_serial_ports():
            self.port.addItem(f"{dev}", dev)
            self.port.setItemData(self.port.count() - 1, desc, 3)  # tooltip role
        if cur:
            self.port.setEditText(cur)

    def config(self) -> TransportConfig:
        kind = self.link.currentData() or "auto"
        ids = []
        for part in self.ids.text().replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit() and 0 < int(part) < 127:
                ids.append(int(part))
        cfg = TransportConfig(profile=self.profile.currentData(), kind=kind,
                              can_prefix=self.prefix.value(), disable_brs=self.brs.isChecked(),
                              explicit_ids=ids,
                              amps_per_digit=self.apd.value(), torque_constant=self.kt.value())
        port = self.port.currentText().strip()
        if kind == "fdcanusb":
            cfg.fdcanusb_path = port
        elif kind == "st_serial":
            cfg.serial_port = port
            cfg.baudrate = int(self.baud.currentText() or 115200)
        elif kind == "socketcan":
            cfg.can_channel = self.sc_chan.text().strip() or "can0"
        elif kind == "pythoncan":
            cfg.can_interface = self.pc_iface.currentText().strip()
            cfg.can_channel = self.pc_chan.text().strip()
        elif kind == "virtual_canfd":
            cfg.virtual_channel = self.v_chan.text().strip() or "239.74.163.2"
            cfg.virtual_port = self.v_port.value()
        return cfg

    def has_link(self, kind: str) -> bool:
        return self.link.findData(kind) >= 0

    def select_link(self, kind: str) -> None:
        i = self.link.findData(kind)
        if i >= 0:
            self.link.setCurrentIndex(i)

    def _launch_sim(self) -> None:
        import os
        import sys
        from PySide6.QtCore import QProcess
        ids = ",".join(p.strip() for p in self.v_ids.text().split(",") if p.strip().isdigit()) or "1,2"
        args = ["--ids", ids, "--channel", self.v_chan.text().strip() or "239.74.163.2",
                "--port", str(self.v_port.value())]
        # Run from the package's parent folder so "-m moteus_hmi.sim" resolves
        # no matter where the HMI was started from.
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if getattr(sys, "frozen", False):
            ok = QProcess.startDetached(sys.executable, ["--simulator", *args], root)
        else:
            ok = QProcess.startDetached(sys.executable, ["-m", "moteus_hmi.sim", *args], root)
        ok = ok[0] if isinstance(ok, tuple) else ok
        self.state.setText("Motor Simulator started – now Connect" if ok else "Could not start the simulator")

    def _toggle(self) -> None:
        if self.ctx.connected:
            self.disconnect_requested.emit()
        else:
            self.connect_requested.emit(self.profile.currentData(), self.config())

    def set_state(self, connected: bool, text: str, busy: bool = False) -> None:
        self.state.setText(text)
        self.led.set_color(theme.WARN if busy else theme.OK if connected else theme.MUTED)
        self.connect_btn.setText("Disconnect" if connected else "Connect")
        self.connect_btn.setProperty("kind", "danger" if connected else "primary")
        self.connect_btn.style().unpolish(self.connect_btn)
        self.connect_btn.style().polish(self.connect_btn)
        for w in (self.profile, self.link, self.stack, self.can_opts):
            w.setEnabled(not connected and not busy)
        self.connect_btn.setEnabled(not busy and self.ctx.can(Perm.CONNECT))

    def on_session(self) -> None:
        self.connect_btn.setEnabled(self.ctx.can(Perm.CONNECT))

    def set_devices(self, devices) -> None:
        self.motors.blockSignals(True)
        self.motors.clear()
        for d in devices:
            it = QListWidgetItem(f"● {d.label()}")
            it.setData(256, d.motor_id)
            self.motors.addItem(it)
        self.motors.blockSignals(False)
        if devices:
            self.motors.setCurrentRow(0)

    def select(self, motor_id: int) -> None:
        for i in range(self.motors.count()):
            if self.motors.item(i).data(256) == motor_id:
                self.motors.blockSignals(True)
                self.motors.setCurrentRow(i)
                self.motors.blockSignals(False)

    def update_status(self, batch) -> None:
        for i in range(self.motors.count()):
            it = self.motors.item(i)
            st = batch.status.get(it.data(256))
            if not st:
                continue
            if batch.estop or st.tripped or not st.comm_ok:
                color, tag = theme.ALARM, "TRIP" if st.tripped else ("E-STOP" if batch.estop else "NO COMM")
            elif st.armed:
                color, tag = theme.ACCENT, "ENABLED"
            else:
                color, tag = theme.TEXT, "disabled"
            it.setText(f"● ID {it.data(256)}   {tag}")
            it.setForeground(QColor(color))

    def _motor_clicked(self, cur, _prev) -> None:
        if cur is not None:
            self.ctx.select_motor(int(cur.data(256)))
