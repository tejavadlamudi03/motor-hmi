"""Feedback & Filters: per-signal noise reduction chain, live raw vs
filtered comparison, noise statistics, data recording."""

from __future__ import annotations

import math

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QListWidget, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from ...core.filters import DEFAULT_FILTERS, validate_chain
from ...core.types import SIGNALS
from ...security.auth import Perm
from .. import theme
from ..widgets import LivePlot, button, group, muted
from .base import HmiTab


class FiltersTab(HmiTab):
    title = "Feedback && Filters"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QGridLayout(self)

        left = QVBoxLayout()
        self.signals = QListWidget()
        for key, (label, unit) in SIGNALS.items():
            self.signals.addItem(f"{label} [{unit}]")
        self.signals.setCurrentRow(1)
        self.signals.currentRowChanged.connect(self._signal_changed)
        left.addWidget(self.signals)

        f = QFormLayout()
        self.spike = QComboBox(); self.spike.addItems(["none", "median", "hampel"])
        self.spike_win = QSpinBox(); self.spike_win.setRange(3, 51); self.spike_win.setSingleStep(2); self.spike_win.setValue(7)
        self.spike_sig = QDoubleSpinBox(); self.spike_sig.setRange(0.5, 10); self.spike_sig.setValue(3.0)
        self.notch_on = QCheckBox("Enable")
        self.notch_f = QDoubleSpinBox(); self.notch_f.setRange(0.1, 250); self.notch_f.setValue(17.0); self.notch_f.setSuffix(" Hz")
        self.notch_q = QDoubleSpinBox(); self.notch_q.setRange(0.3, 50); self.notch_q.setValue(4.0)
        self.smooth = QComboBox(); self.smooth.addItems(["none", "ema", "butterworth", "kalman"])
        self.cutoff = QDoubleSpinBox(); self.cutoff.setRange(0.05, 250); self.cutoff.setValue(15.0); self.cutoff.setSuffix(" Hz")
        self.kq = QDoubleSpinBox(); self.kq.setDecimals(5); self.kq.setRange(1e-5, 1e4); self.kq.setValue(1.0)
        self.kr = QDoubleSpinBox(); self.kr.setDecimals(5); self.kr.setRange(1e-5, 1e4); self.kr.setValue(0.01)
        f.addRow(QLabel("<b>1 · Spike rejection</b>"))
        f.addRow("Type", self.spike)
        f.addRow("Window", self.spike_win)
        f.addRow("Threshold (σ)", self.spike_sig)
        f.addRow(QLabel("<b>2 · Notch</b>"))
        f.addRow(self.notch_on)
        f.addRow("Centre", self.notch_f)
        f.addRow("Q", self.notch_q)
        f.addRow(QLabel("<b>3 · Smoothing</b>"))
        f.addRow("Type", self.smooth)
        f.addRow("Cut-off", self.cutoff)
        f.addRow("Kalman q", self.kq)
        f.addRow("Kalman r", self.kr)
        left.addLayout(f)
        row = QHBoxLayout()
        self.scope = QComboBox(); self.scope.addItems(["All motors", "Selected motor"])
        self.apply_btn = button("Apply", "primary", self._apply)
        self.default_btn = button("Defaults", slot=self._defaults)
        row.addWidget(self.scope)
        row.addWidget(self.apply_btn)
        row.addWidget(self.default_btn)
        left.addLayout(row)
        self.err = QLabel("")
        self.err.setStyleSheet(f"color: {theme.ALARM};")
        left.addWidget(self.err)
        lay.addWidget(group("Filter chain (HMI side)", left), 0, 0, 2, 1)

        self.plot = LivePlot("Filtered", "", theme.TRACE["filtered"])
        self.plot.add_series("raw", theme.TRACE["raw"], 1)
        self.plot.curves["raw"].setZValue(-1)
        pl = QVBoxLayout()
        pl.addWidget(self.plot)
        pl.addWidget(muted("Grey = raw feedback, blue = filtered. Filters clean the display, the "
                           "recorded *_filt columns and the HMI alarm checks (spike rejection prevents "
                           "nuisance trips). They do not change the controller's internal loop – use "
                           "Calibration › controller-side noise reduction for that."))
        lay.addWidget(group("Raw vs filtered", pl), 0, 1)

        nt = QVBoxLayout()
        self.noise = QTableWidget(len(SIGNALS), 4)
        self.noise.setHorizontalHeaderLabels(["Signal", "Raw noise σ", "Filtered σ", "Reduction"])
        self.noise.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for r, (k, (label, unit)) in enumerate(SIGNALS.items()):
            self.noise.setItem(r, 0, QTableWidgetItem(f"{label} [{unit}]"))
        nt.addWidget(self.noise)
        rec = QHBoxLayout()
        self.rec_btn = button("● Start recording", slot=self._toggle_record)
        self.rec_lbl = muted("Not recording", wrap=False)
        rec.addWidget(self.rec_btn)
        rec.addWidget(self.rec_lbl, 1)
        nt.addLayout(rec)
        lay.addWidget(group("Noise statistics (high-pass σ estimate, last 2 s)", nt), 1, 1)
        lay.setColumnStretch(1, 3)
        lay.setRowStretch(0, 3)
        lay.setRowStretch(1, 2)
        self.recording = False
        self._signal_changed(1)
        self.on_session()

    # ------------------------------------------------------------------
    def _key(self) -> str:
        return list(SIGNALS)[max(0, self.signals.currentRow())]

    def _signal_changed(self, _row: int) -> None:
        key = self._key()
        label, unit = SIGNALS[key]
        self.plot.title_text = label
        self.plot.set_unit(unit)
        self.plot.clear()
        specs = self.ctx.settings.filters.get(key, DEFAULT_FILTERS.get(key, []))
        self._load(specs)

    def _load(self, specs) -> None:
        self.spike.setCurrentText("none")
        self.notch_on.setChecked(False)
        self.smooth.setCurrentText("none")
        for s in specs:
            t = s.get("type")
            if t in ("median", "hampel"):
                self.spike.setCurrentText(t)
                self.spike_win.setValue(int(s.get("window", 7)))
                self.spike_sig.setValue(float(s.get("n_sigma", 3.0)))
            elif t == "notch":
                self.notch_on.setChecked(True)
                self.notch_f.setValue(float(s.get("center_hz", 50)))
                self.notch_q.setValue(float(s.get("q", 4)))
            elif t in ("ema", "butterworth", "kalman"):
                self.smooth.setCurrentText(t)
                self.cutoff.setValue(float(s.get("cutoff_hz", self.cutoff.value())))
                self.kq.setValue(float(s.get("process_noise", self.kq.value())))
                self.kr.setValue(float(s.get("measurement_noise", self.kr.value())))

    def _specs(self) -> list[dict]:
        specs = []
        if self.spike.currentText() == "median":
            w = self.spike_win.value() | 1
            specs.append({"type": "median", "window": w})
        elif self.spike.currentText() == "hampel":
            specs.append({"type": "hampel", "window": self.spike_win.value(), "n_sigma": self.spike_sig.value()})
        if self.notch_on.isChecked():
            specs.append({"type": "notch", "center_hz": self.notch_f.value(), "q": self.notch_q.value()})
        t = self.smooth.currentText()
        if t in ("ema", "butterworth"):
            specs.append({"type": t, "cutoff_hz": self.cutoff.value()})
        elif t == "kalman":
            specs.append({"type": "kalman", "process_noise": self.kq.value(), "measurement_noise": self.kr.value()})
        return specs

    def _apply(self) -> None:
        if not self.ctx.require(Perm.EDIT_LIMITS, self):
            return
        specs = self._specs()
        err = validate_chain(specs, self.ctx.worker.rate_hz)
        self.err.setText(err or "")
        if err:
            return
        key = self._key()
        mid = self.ctx.selected_motor if self.scope.currentIndex() == 1 else None
        self.ctx.settings.filters[key] = specs
        self.ctx.save_settings()
        self.ctx.audit("filters.set", {"signal": key, "motor": mid or "all", "chain": specs})
        self.ctx.run(self.ctx.worker.set_filter(mid, key, specs), what="Apply filter",
                     ok=lambda _: self.ctx.status_message.emit(f"Filter applied to {SIGNALS[key][0]}", 3000))
        self.plot.clear()

    def _defaults(self) -> None:
        self._load(DEFAULT_FILTERS.get(self._key(), []))

    def on_session(self) -> None:
        self.apply_btn.setEnabled(self.ctx.can(Perm.EDIT_LIMITS))
        self.rec_btn.setEnabled(self.ctx.connected and self.ctx.can(Perm.RECORD))

    def on_connection(self, connected: bool) -> None:
        if not connected and self.recording:
            self.recording = False
            self.rec_btn.setText("● Start recording")
            self.rec_lbl.setText("Recording stopped (disconnected)")
        self.on_session()

    def on_batch(self, batch) -> None:
        mid = self.ctx.selected_motor
        key = self._key()
        last = None
        for s in batch.samples.get(mid, []):
            raw = s.raw.to_dict()[key] if key in ("position_error", "velocity_error") else getattr(s.raw, key)
            self.plot.append(s.raw.timestamp, {"main": s.filtered[key], "raw": raw})
            last = s.filtered[key]
        self.plot.redraw(last)

    def on_noise(self, motor_id: int, reports) -> None:
        if motor_id != self.ctx.selected_motor:
            return
        for r, rep in enumerate(reports):
            def f(v):
                return "—" if not math.isfinite(v) else f"{v:.3g}"
            db = rep.reduction_db
            self.noise.setItem(r, 1, QTableWidgetItem(f(rep.raw_std)))
            self.noise.setItem(r, 2, QTableWidgetItem(f(rep.filtered_std)))
            self.noise.setItem(r, 3, QTableWidgetItem("—" if not math.isfinite(db) else f"{db:.1f} dB"))

    def _toggle_record(self) -> None:
        if not self.ctx.require(Perm.RECORD, self):
            return
        if not self.recording:
            def ok(path):
                self.recording = True
                self.rec_btn.setText("■ Stop recording")
                self.rec_lbl.setText(f"Recording → {path}")
                self.ctx.audit("data.record_start", {"file": path})
            self.ctx.run(self.ctx.worker.start_recording(self.ctx.username), ok=ok, what="Recording")
        else:
            def ok(path):
                self.recording = False
                self.rec_btn.setText("● Start recording")
                self.rec_lbl.setText(f"Saved {path} (+ .sha256)")
                self.ctx.audit("data.record_stop", {"file": path})
            self.ctx.run(self.ctx.worker.stop_recording(), ok=ok, what="Recording")
