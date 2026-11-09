"""Calibration: motor / encoder calibration, zero offset, encoder filter."""

from __future__ import annotations

import json
import math
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout,
                               QLabel, QListWidget, QMessageBox, QPlainTextEdit, QScrollArea,
                               QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
                               QHeaderView)

from ...core.controllers import F_CAL_TOOL, F_CONF, F_CONF_PERSIST, F_ENCODER_ALIGN, F_SET_OUTPUT
from ...core.tools import CalibrationOptions, ToolRunner
from ...core.types import ControlMode, MotorCommand
from ...security.auth import Perm
from .. import theme
from ..widgets import button, confirm, group, muted
from .base import HmiTab

CHECKLIST = [
    "The motor shaft can spin freely in both directions (no load, no gearbox end stops).",
    "Guards are in place and nobody is touching the motor or mechanism.",
    "The supply current limit is set appropriately for this motor.",
    "The motor is clamped so it cannot walk or fall when it spins.",
    "I am authorised to commission this axis.",
]

FILTER_KEYS = [
    ("motor_position.sources.0.pll_filter_hz", "Encoder PLL filter (Hz)",
     "Low-pass on the encoder. Lower = less audible noise / jitter, higher = more bandwidth. "
     "Keep below 10× the encoder update rate and above the mechanical bandwidth."),
    ("servo.pid_dq_hz", "Current loop bandwidth (Hz)",
     "Set by calibration (--cal-bw-hz). Higher = faster torque, more audible noise."),
    ("servo.max_current_desired_rate", "Max current slew (A/s)",
     "Limits how fast commanded current changes; smooths torque steps."),
]


class CalibrationTab(HmiTab):
    title = "Calibration"
    perm = Perm.VIEW
    _line = Signal(str)           # thread-safe log line (simulation path)

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        lay = QGridLayout(body)

        # ---- motor / encoder calibration ------------------------------
        cal = QVBoxLayout()
        self.cal_note = muted("")
        cal.addWidget(self.cal_note)
        self.checks = [QCheckBox(t) for t in CHECKLIST]
        for c in self.checks:
            c.toggled.connect(self.on_session)
            cal.addWidget(c)
        form = QFormLayout()
        self.o_power = QDoubleSpinBox(); self.o_power.setRange(0.5, 100); self.o_power.setValue(7.5); self.o_power.setSuffix(" W")
        self.o_bw = QDoubleSpinBox(); self.o_bw.setRange(20, 2000); self.o_bw.setValue(200); self.o_bw.setSuffix(" Hz")
        self.o_enc_bw = QDoubleSpinBox(); self.o_enc_bw.setRange(0, 5000); self.o_enc_bw.setSpecialValueText("auto"); self.o_enc_bw.setSuffix(" Hz")
        self.o_poles = QSpinBox(); self.o_poles.setRange(0, 200); self.o_poles.setSpecialValueText("auto")
        self.o_kv = QDoubleSpinBox(); self.o_kv.setRange(0, 5000); self.o_kv.setSpecialValueText("measure"); self.o_kv.setSuffix(" rpm/V")
        self.o_hall = QCheckBox("Motor uses hall sensors")
        self.o_invert = QCheckBox("Invert direction")
        self.o_dry = QCheckBox("Dry run (--cal-no-update, keep current config)")
        form.addRow("Calibration power", self.o_power)
        form.addRow("Torque bandwidth", self.o_bw)
        form.addRow("Encoder filter BW", self.o_enc_bw)
        form.addRow("Motor poles", self.o_poles)
        form.addRow("Known Kv (limits rotation)", self.o_kv)
        form.addRow(self.o_hall)
        form.addRow(self.o_invert)
        form.addRow(self.o_dry)
        cal.addLayout(form)
        row = QHBoxLayout()
        self.run_btn = button("Run calibration", "danger", self._run_cal)
        self.align_btn = button("Encoder alignment", "danger", self._align,
                                tip="ST MCSDK: execute ENCODER_ALIGN")
        self.abort_btn = button("Abort", slot=self._abort)
        self.abort_btn.setEnabled(False)
        row.addWidget(self.run_btn)
        row.addWidget(self.align_btn)
        row.addWidget(self.abort_btn)
        row.addStretch(1)
        cal.addLayout(row)
        lay.addWidget(group("Motor & encoder calibration", cal), 0, 0)

        # ---- output / log ----------------------------------------------
        out = QVBoxLayout()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family: monospace; font-size: 9pt;")
        out.addWidget(self.log, 2)
        self.report = QTableWidget(0, 2)
        self.report.setHorizontalHeaderLabels(["Result", "Value"])
        self.report.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        out.addWidget(self.report, 1)
        lay.addWidget(group("Calibration output", out), 0, 1, 2, 1)

        # ---- zero / offset -----------------------------------------------
        z = QGridLayout()
        self.z_value = QDoubleSpinBox(); self.z_value.setRange(-1000, 1000); self.z_value.setDecimals(5); self.z_value.setSuffix(" rev")
        self.z_exact = button("Set output = value (temporary)", slot=lambda: self._set_output(True))
        self.z_nearest = button("Set output nearest", slot=lambda: self._set_output(False),
                                tip="Keep the whole-turn count closest to the current reading")
        self.z_persist = button("Save as zero offset (flash)", "danger", self._persist_offset)
        z.addWidget(QLabel("Output position"), 0, 0)
        z.addWidget(self.z_value, 0, 1)
        z.addWidget(self.z_exact, 1, 0)
        z.addWidget(self.z_nearest, 1, 1)
        z.addWidget(self.z_persist, 2, 0, 1, 2)
        z.addWidget(muted("The motor must be disabled. Temporary settings are lost at power-off."), 3, 0, 1, 2)
        lay.addWidget(group("Zero / output reference", z), 1, 0)

        # ---- hardware filter ----------------------------------------------
        f = QGridLayout()
        self.f_spins: dict[str, QDoubleSpinBox] = {}
        for r, (key, label, help_) in enumerate(FILTER_KEYS):
            sp = QDoubleSpinBox(); sp.setRange(0, 1e6); sp.setDecimals(1)
            self.f_spins[key] = sp
            f.addWidget(QLabel(label), r * 2, 0)
            f.addWidget(sp, r * 2, 1)
            f.addWidget(muted(help_), r * 2 + 1, 0, 1, 2)
        fr = QHBoxLayout()
        self.f_read = button("Read", slot=self._read_filters)
        self.f_write = button("Write", slot=self._write_filters)
        self.f_save = button("Save to flash", "danger", self._save_flash)
        for b in (self.f_read, self.f_write, self.f_save):
            fr.addWidget(b)
        f.addLayout(fr, len(FILTER_KEYS) * 2, 0, 1, 2)
        lay.addWidget(group("Controller-side noise reduction (inside the 40 kHz loop)", f), 2, 0)

        hist = QVBoxLayout()
        self.history = QListWidget()
        self.history.itemDoubleClicked.connect(self._open_report)
        hist.addWidget(self.history)
        lay.addWidget(group("Calibration history (double-click to view)", hist), 2, 1)
        lay.setColumnStretch(0, 1)
        lay.setColumnStretch(1, 1)

        self.runner = ToolRunner(ctx.keystore, ctx.paths.calibration, self)
        self.runner.line.connect(self.log.appendPlainText)
        self.runner.finished.connect(self._tool_done)
        self._line.connect(self.log.appendPlainText)
        self._cal_target = None
        self._refresh_history()
        self.on_session()

    # ------------------------------------------------------------------
    def on_connection(self, connected: bool) -> None:
        self.on_session()

    def on_session(self, *_):
        c = self.ctx
        prof = c.profile
        conn = c.connected
        busy = self.runner.running or self._cal_target is not None
        checks_ok = all(x.isChecked() for x in self.checks)
        can_cal = c.can(Perm.CALIBRATE)
        virtual = conn and c.worker.config.kind == "virtual_canfd"
        self.run_btn.setVisible(prof.has(F_CAL_TOOL))
        self.align_btn.setVisible(prof.has(F_ENCODER_ALIGN))
        self.run_btn.setEnabled(conn and can_cal and checks_ok and not busy and not virtual)
        self.align_btn.setEnabled(conn and can_cal and checks_ok and not busy)
        self.abort_btn.setEnabled(busy)
        so = prof.has(F_SET_OUTPUT)
        for b in (self.z_exact, self.z_nearest):
            b.setEnabled(conn and so and can_cal and not busy)
        self.z_persist.setEnabled(conn and so and can_cal and prof.has(F_CONF_PERSIST) and not busy)
        conf = prof.has(F_CONF) and prof.driver == "moteus"
        self.f_read.setEnabled(conn and conf and not busy)
        self.f_write.setEnabled(conn and conf and c.can(Perm.CONFIGURE) and not busy)
        self.f_save.setEnabled(conn and conf and c.can(Perm.CONFIGURE) and not busy)
        if virtual:
            self.cal_note.setText("Motor Simulator: the bus protocol is simulated, the firmware calibration "
                                  "routines are not. Run calibration on the real controller.")
        elif prof.has(F_CAL_TOOL):
            self.cal_note.setText("Runs the official moteus_tool --calibrate. The HMI releases the "
                                  "CAN adapter during calibration and re-connects afterwards.")
        else:
            self.cal_note.setText("ST MCSDK: motor parameters are identified with ST Motor Profiler "
                                  "(MC Workbench). Encoder alignment can be run from here.")

    def _refresh_history(self) -> None:
        self.history.clear()
        files = sorted(Path(self.ctx.paths.calibration).glob("*.log"), reverse=True)
        files += sorted(Path(self.ctx.paths.calibration).glob("*.json"), reverse=True)
        for p in files[:200]:
            self.history.addItem(p.name)

    def _open_report(self, item) -> None:
        p = Path(self.ctx.paths.calibration) / item.text()
        try:
            self._show_report(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Report", str(e))

    def _show_report(self, rep: dict) -> None:
        rows = []
        for k in ("timestamp", "winding_resistance", "inductance", "kv", "torque_bw_hz",
                  "encoder_filter_bw_hz", "voltage_mode_control", "abi_version", "simulated"):
            if k in rep:
                rows.append((k, rep[k]))
        di = rep.get("device_info", {})
        rows += [(f"device.{k}", v) for k, v in di.items()]
        cal = rep.get("calibration", {})
        if isinstance(cal, dict):
            rows += [(f"cal.{k}", v) for k, v in cal.items() if not isinstance(v, (list, dict))]
        self.report.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            self.report.setItem(r, 0, QTableWidgetItem(str(k)))
            self.report.setItem(r, 1, QTableWidgetItem(f"{v:.6g}" if isinstance(v, float) else str(v)))

    def _options(self, target: int) -> CalibrationOptions:
        return CalibrationOptions(
            target=target, cal_motor_power=self.o_power.value(), cal_bw_hz=self.o_bw.value(),
            encoder_bw_hz=self.o_enc_bw.value() or None, cal_motor_poles=self.o_poles.value() or None,
            cal_force_kv=self.o_kv.value() or None, cal_hall=self.o_hall.isChecked(),
            cal_invert=self.o_invert.isChecked(), cal_no_update=self.o_dry.isChecked())

    # ---- calibration flow --------------------------------------------
    def _run_cal(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.CALIBRATE, self):
            return
        if not confirm(self, "Calibrate motor", f"Controller {mid} will spin the motor in both "
                       "directions, at speed. Keep clear of all moving parts.", typed="CALIBRATE"):
            return
        opts = self._options(mid)
        self.log.clear()
        self.report.setRowCount(0)
        self.ctx.audit("calibration.start", {"motor": mid, "options": opts.to_dict()})
        w = self.ctx.worker
        self._cal_target = mid
        self.on_session()
        from ...core.moteus_backend import MoteusBackend
        tool_args = MoteusBackend(w.config).moteus_tool_args() + opts.to_args()

        def released(_):
            self.log.appendPlainText("CAN adapter released; starting moteus_tool…")
            try:
                self.runner.start(tool_args)
            except RuntimeError as e:
                self._tool_done(1, None, str(e))

        self.ctx.run(w.release_bus(), ok=released, err=lambda e: self._tool_done(1, None, e))

    def _abort(self) -> None:
        self.ctx.audit("calibration.abort", {"motor": self._cal_target})
        if self.runner.running:
            self.runner.kill()
        else:
            self.ctx.worker.estop("calibration aborted")

    def _tool_done(self, code: int, report, error: str = "") -> None:
        mid = self._cal_target
        self._cal_target = None
        ok = code == 0 and report is not None
        if error:
            self.log.appendPlainText(f"ERROR: {error}")
        if report:
            self._show_report(report)
            if report.get("simulated"):
                name = f"sim-cal-motor{mid}-{report.get('timestamp', '').replace(' ', 'T').replace(':', '')}.json"
                (Path(self.ctx.paths.calibration) / name).write_text(json.dumps(report, indent=2), encoding="utf-8")
        self.ctx.audit("calibration.finish", {"motor": mid, "exit_code": code,
                                              "kv": (report or {}).get("kv"),
                                              "resistance": (report or {}).get("winding_resistance")},
                       "ok" if ok else "failed")
        self._refresh_history()
        w = self.ctx.worker
        if not w.connected:
            self.log.appendPlainText("Re-connecting to the bus and commanding stop…")

            def back(ids):
                for i in ids:
                    w.set_command(i, MotorCommand(mode=ControlMode.STOP))
                self.log.appendPlainText("Bus re-acquired.")

            self.ctx.run(w.reacquire_bus(), ok=back, what="Re-connect after calibration")
        if ok:
            QMessageBox.information(self, "Calibration", "Calibration complete." +
                                    (" (dry run – configuration unchanged)" if self.o_dry.isChecked() else ""))
        else:
            QMessageBox.warning(self, "Calibration", "Calibration did not complete. See the log.")
        self.on_session()

    def _align(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.CALIBRATE, self):
            return
        if not confirm(self, "Encoder alignment", "The rotor will be pulled to the alignment angle.",
                       typed="ALIGN"):
            return
        self.ctx.audit("calibration.encoder_align", {"motor": mid})
        self.ctx.run(self.ctx.worker.diagnostic(mid, "cmd encoder_align"), what="Encoder alignment",
                     ok=lambda _: self.log.appendPlainText("Encoder alignment command accepted."))

    # ---- zero / offset -----------------------------------------------
    def _set_output(self, exact: bool) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.CALIBRATE, self):
            return
        v = self.z_value.value()
        self.ctx.audit("calibration.set_output", {"motor": mid, "value": v, "exact": exact})
        self.ctx.run(self.ctx.worker.set_output(mid, v, exact), what="Set output",
                     ok=lambda _: self.ctx.status_message.emit(f"Output position set to {v:+.5f} rev", 5000))

    def _persist_offset(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.CALIBRATE, self):
            return
        v = self.z_value.value()
        if not confirm(self, "Save zero offset", f"Store an output offset so the current position "
                       f"reads {v:+.5f} rev after every power-up?"):
            return
        w = self.ctx.worker

        async def persist():
            await w._diagnostic(mid, f"d cfg-set-output {v:.6f}", False, 5.0)
            await w._diagnostic(mid, "conf write", False, 5.0)
            return True

        self.ctx.audit("calibration.persist_offset", {"motor": mid, "value": v})
        self.ctx.run(w.run_coroutine(persist), what="Save offset",
                     ok=lambda _: self.ctx.status_message.emit("Offset saved to flash", 5000))

    # ---- hardware filters --------------------------------------------
    def _read_filters(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None:
            return
        for key, sp in self.f_spins.items():
            self.ctx.run(self.ctx.worker.diagnostic(mid, f"conf get {key}", True),
                         ok=lambda v, sp=sp: sp.setValue(float(v) if v.strip().lower() != "nan" else 0.0),
                         err=lambda e, sp=sp: sp.setEnabled(False))

    def _write_filters(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.CONFIGURE, self):
            return
        vals = {k: sp.value() for k, sp in self.f_spins.items() if sp.isEnabled()}
        if vals.get("motor_position.sources.0.pll_filter_hz", 1) <= 0:
            if not confirm(self, "Disable encoder filter", "A PLL filter of 0 disables the encoder "
                           "filter and most encoders then report NO velocity. Continue?"):
                return
        w = self.ctx.worker

        async def write():
            for k, v in vals.items():
                await w._diagnostic(mid, f"conf set {k} {v:g}", False, 5.0)
            return True

        self.ctx.audit("config.filters", {"motor": mid, "values": vals})
        self.ctx.run(w.run_coroutine(write), what="Write filters",
                     ok=lambda _: self.ctx.status_message.emit("Filter parameters written (RAM)", 5000))

    def _save_flash(self) -> None:
        mid = self.ctx.selected_motor
        if mid is None or not self.ctx.require(Perm.CONFIGURE, self):
            return
        if confirm(self, "Save to flash", "Save the controller configuration to flash?"):
            self.ctx.audit("config.write", {"motor": mid})
            self.ctx.run(self.ctx.worker.diagnostic(mid, "conf write"), what="conf write",
                         ok=lambda _: self.ctx.status_message.emit("Saved to flash", 4000))
