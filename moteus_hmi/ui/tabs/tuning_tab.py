"""Tuning: controller gains + step-response test with metrics."""

from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6.QtWidgets import (QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ...core.config_io import validate_kv
from ...core.controllers import F_CONF, F_CONF_PERSIST, F_STEP_TEST
from ...security.auth import Perm
from .. import theme
from ..widgets import button, confirm, group, muted
from .base import HmiTab

MOTEUS_GAINS = [
    ("servo.pid_position.kp", "Kp [N·m/rev]"),
    ("servo.pid_position.ki", "Ki [N·m/(rev·s)]"),
    ("servo.pid_position.kd", "Kd [N·m/(rev/s)]"),
    ("servo.pid_position.ilimit", "I limit [N·m]"),
    ("servo.pid_position.iratelimit", "I rate limit [N·m/s]"),
    ("servo.default_velocity_limit", "Default velocity limit [rev/s]"),
    ("servo.default_accel_limit", "Default accel limit [rev/s²]"),
    ("servo.inertia_feedforward", "Inertia feed-forward [kg·m²]"),
]
ST_GAINS = [
    ("mcsdk.speed_kp", "Speed Kp"), ("mcsdk.speed_ki", "Speed Ki"),
    ("mcsdk.torque_kp", "Iq Kp"), ("mcsdk.torque_ki", "Iq Ki"),
    ("mcsdk.flux_kp", "Id Kp"), ("mcsdk.flux_ki", "Id Ki"),
]


class TuningTab(HmiTab):
    title = "Tuning"
    perm = Perm.VIEW

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QGridLayout(self)
        g = QVBoxLayout()
        self.form = QFormLayout()
        self.fields: dict[str, QLineEdit] = {}
        g.addLayout(self.form)
        row = QHBoxLayout()
        self.read_btn = button("Read", slot=self._read)
        self.write_btn = button("Write (RAM)", "primary", self._write)
        self.save_btn = button("Save to flash", "danger", self._save)
        for b in (self.read_btn, self.write_btn, self.save_btn):
            row.addWidget(b)
        g.addLayout(row)
        g.addWidget(muted("Gains act on the controller's internal loop. Change in small steps, "
                          "verify with a step test, then save."))
        self.gain_box = group("Controller gains", g)
        lay.addWidget(self.gain_box, 0, 0)

        s = QFormLayout()
        self.amp = QDoubleSpinBox(); self.amp.setRange(-5, 5); self.amp.setDecimals(4); self.amp.setValue(0.1); self.amp.setSuffix(" rev")
        self.dur = QDoubleSpinBox(); self.dur.setRange(0.2, 10); self.dur.setValue(1.5); self.dur.setSuffix(" s")
        self.vlim = QDoubleSpinBox(); self.vlim.setRange(0.01, 100); self.vlim.setValue(1.0); self.vlim.setSuffix(" rev/s")
        self.alim = QDoubleSpinBox(); self.alim.setRange(0.01, 1000); self.alim.setValue(10.0); self.alim.setSuffix(" rev/s²")
        s.addRow("Step amplitude", self.amp)
        s.addRow("Record time", self.dur)
        s.addRow("Velocity limit", self.vlim)
        s.addRow("Accel limit", self.alim)
        self.step_btn = button("Run step test", "primary", self._step)
        s.addRow(self.step_btn)
        s.addRow(muted("The motor must be enabled. It moves by the amplitude and returns. "
                       "All safety limits still apply."))
        self.metrics = QTableWidget(0, 2)
        self.metrics.setHorizontalHeaderLabels(["Metric", "Value"])
        self.metrics.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        s.addRow(self.metrics)
        self.step_box = group("Step response", s)
        lay.addWidget(self.step_box, 1, 0)

        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend()
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.setLabel("left", "position", units="rev")
        self.c_y = self.plot.plot(pen=pg.mkPen(theme.TRACE["position"], width=2), name="measured")
        self.c_ctrl = self.plot.plot(pen=pg.mkPen(theme.TRACE["command"], width=1, style=pg.QtCore.Qt.DashLine),
                                     name="trajectory")
        self.c_tgt = self.plot.plot(pen=pg.mkPen(theme.MUTED, width=1, style=pg.QtCore.Qt.DotLine), name="target")
        lay.addWidget(group("Step plot", self._wrap(self.plot)), 0, 1, 2, 1)
        lay.setColumnStretch(1, 2)
        self._build_fields()
        self.on_session()

    @staticmethod
    def _wrap(w):
        l = QVBoxLayout()
        l.addWidget(w)
        return l

    def _gains(self):
        return MOTEUS_GAINS if self.ctx.profile.driver == "moteus" else ST_GAINS

    def _build_fields(self) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.fields = {}
        for key, label in self._gains():
            e = QLineEdit()
            e.setPlaceholderText("read from controller")
            e.setToolTip(key)
            self.fields[key] = e
            self.form.addRow(label, e)

    def on_connection(self, connected: bool) -> None:
        self._build_fields()
        self.on_session()

    def on_session(self) -> None:
        c = self.ctx
        prof = c.profile
        conn = c.connected and prof.has(F_CONF)
        tune = c.can(Perm.TUNE)
        self.read_btn.setEnabled(conn)
        self.write_btn.setEnabled(conn and tune)
        self.save_btn.setEnabled(conn and tune and prof.has(F_CONF_PERSIST))
        self.step_box.setVisible(prof.has(F_STEP_TEST))
        self.step_btn.setEnabled(c.connected and tune and c.can(Perm.OPERATE))
        for e in self.fields.values():
            e.setReadOnly(not tune)

    def _read(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None:
            return
        for key, e in self.fields.items():
            self.ctx.run(self.ctx.worker.diagnostic(mid, f"conf get {key}", True),
                         ok=lambda v, e=e: e.setText(v.strip()),
                         err=lambda msg, e=e: e.setPlaceholderText("not supported"))

    def _write(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.TUNE, self):
            return
        items = [(k, e.text().strip()) for k, e in self.fields.items() if e.text().strip()]
        bad = [m for k, v in items if (m := validate_kv(k, v))]
        if bad:
            self.ctx.status_message.emit("; ".join(bad), 8000)
            return
        w = self.ctx.worker

        async def write():
            errs = []
            for k, v in items:
                try:
                    await w._diagnostic(mid, f"conf set {k} {v}", False, 5.0)
                except Exception as ex:
                    errs.append(f"{k}: {ex}")
            return errs

        self.ctx.audit("tuning.write", {"motor": mid, "gains": dict(items)})
        self.ctx.run(w.run_coroutine(write), what="Write gains",
                     ok=lambda errs: self.ctx.status_message.emit(
                         "Gains written to RAM" if not errs else "Rejected: " + "; ".join(errs), 8000))

    def _save(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.TUNE, self):
            return
        if confirm(self, "Save gains", "Save the controller configuration (including gains) to flash?"):
            self.ctx.audit("config.write", {"motor": mid, "source": "tuning"})
            self.ctx.run(self.ctx.worker.diagnostic(mid, "conf write"), what="conf write",
                         ok=lambda _: self.ctx.status_message.emit("Saved to flash", 4000))

    def _step(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.TUNE, self):
            return
        if not confirm(self, "Step test", f"Motor {mid} will move {self.amp.value():+.4f} rev and back."):
            return
        self.step_btn.setEnabled(False)
        self.ctx.audit("tuning.step_test", {"motor": mid, "amplitude": self.amp.value()})

        def done(res):
            self.step_btn.setEnabled(True)
            t = res["t"]
            self.c_y.setData(t, res["y"], connect="finite")
            self.c_ctrl.setData(t, res["control"], connect="finite")
            self.c_tgt.setData([0, t[-1] if t else 1], [res["target"]] * 2)
            rows = res["metrics"].as_rows()
            self.metrics.setRowCount(len(rows))
            for r, (k, v) in enumerate(rows):
                self.metrics.setItem(r, 0, QTableWidgetItem(k))
                self.metrics.setItem(r, 1, QTableWidgetItem(v))

        def fail(e):
            self.step_btn.setEnabled(True)
            self.ctx.status_message.emit(f"Step test failed: {e}", 8000)

        self.ctx.run(self.ctx.worker.run_step_test(mid, self.amp.value(), self.dur.value(),
                                                   self.vlim.value(), self.alim.value()), ok=done, err=fail)
