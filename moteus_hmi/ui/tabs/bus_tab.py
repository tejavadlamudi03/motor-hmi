"""Bus & Devices: discovered controllers + raw frame monitor."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QPlainTextEdit, QSplitter, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ...core.faults import fault_title
from ...core.types import FirmwareMode, finite
from ...security.auth import Perm
from .. import theme
from ..widgets import button, group, muted
from .base import HmiTab

COLS = ["ID", "UUID / board", "Bus", "Comm", "Enabled", "State", "Fault", "Bus V", "Board °C"]


class BusTab(HmiTab):
    title = "Bus && Devices"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.scan_btn = button("Scan bus", slot=self._scan, tip="Re-run discovery on the open adapter")
        top.addWidget(self.scan_btn)
        top.addWidget(muted("Every controller on a bus needs a unique ID and the bus needs 120 Ω "
                            "termination at both ends.", wrap=False))
        top.addStretch(1)
        lay.addLayout(top)
        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemSelectionChanged.connect(self._row_selected)
        split.addWidget(self.table)

        mon = QWidget()
        ml = QVBoxLayout(mon)
        ml.setContentsMargins(0, 0, 0, 0)
        opts = QHBoxLayout()
        self.enable_mon = QCheckBox("Capture frames")
        self.show_text = QCheckBox("Show text")
        self.show_text.setChecked(True)
        self.fmt = QComboBox()
        self.fmt.addItems(["HEX", "ASCII"])
        self.scope = QComboBox()
        self.scope.addItems(["Selected motor", "All motors"])
        self.only_changes = QCheckBox("Only when frame changes")
        self.only_changes.setChecked(True)
        for w in (self.enable_mon, self.show_text, QLabel("Format"), self.fmt, self.scope, self.only_changes):
            opts.addWidget(w)
        opts.addStretch(1)
        opts.addWidget(button("Clear", slot=lambda: self.log.clear()))
        ml.addLayout(opts)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setStyleSheet("font-family: monospace; font-size: 9pt;")
        ml.addWidget(self.log)
        split.addWidget(group("Frame monitor (TX = HMI → controller, RX = controller → HMI)", ml))
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        lay.addWidget(split)
        self._last: dict[tuple[str, int], str] = {}
        self._t0 = time.monotonic()
        self.on_session()

    def on_session(self) -> None:
        self.scan_btn.setEnabled(self.ctx.connected and self.ctx.can(Perm.CONNECT))

    def on_connection(self, connected: bool) -> None:
        if not connected:
            self.table.setRowCount(0)
        self.on_session()

    def on_devices(self, devices) -> None:
        self.table.setRowCount(len(devices))
        for r, d in enumerate(devices):
            for c, v in enumerate([d.motor_id, d.uuid[:24], d.bus[:40]]):
                self.table.setItem(r, c, QTableWidgetItem(str(v)))
        self.on_session()

    def _row_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            item = self.table.item(rows[0].row(), 0)
            if item:
                self.ctx.select_motor(int(item.text()))

    def _scan(self) -> None:
        if not self.ctx.require(Perm.CONNECT, self):
            return
        self.ctx.audit("bus.scan")
        self.ctx.run(self.ctx.worker.rediscover(), what="Bus scan",
                     ok=lambda ids: self.ctx.status_message.emit(f"Found controllers: {ids}", 5000))

    def _render(self, data: str) -> str:
        if self.fmt.currentText() == "HEX" or data.startswith("("):
            return data
        try:
            raw = bytes.fromhex(data.replace(" ", ""))
            return "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
        except ValueError:
            return data

    def on_batch(self, batch) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if not item:
                continue
            st = batch.status.get(int(item.text()))
            if not st:
                continue
            s = st.sample
            vals = ["OK" if st.comm_ok else "LOST", "Yes" if st.armed else "No"]
            if s:
                raw = s.raw
                vals += [raw.state_text or FirmwareMode.name_of(raw.mode),
                         raw.fault_text or (fault_title(raw.fault) if raw.fault else "—"),
                         f"{raw.voltage:.1f}" if finite(raw.voltage) else "—",
                         f"{raw.temperature:.1f}" if finite(raw.temperature) else "—"]
            for c, v in enumerate(vals, start=3):
                it = self.table.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.table.setItem(r, c, it)
                it.setText(v)
                bad = (c == 3 and v == "LOST") or (c == 5 and v.startswith("Fault"))
                it.setForeground(Qt.red if bad else Qt.white)
        if not self.enable_mon.isChecked() or not self.show_text.isChecked():
            return
        ids = list(batch.status) if self.scope.currentIndex() == 1 else [self.ctx.selected_motor]
        t = time.monotonic() - self._t0
        lines = []
        for mid in ids:
            for kind in ("tx", "rx"):
                data = batch.bus.get(f"last_{kind}", {}).get(mid, "")
                if not data:
                    continue
                key = (kind, mid)
                if self.only_changes.isChecked() and self._last.get(key) == data:
                    continue
                self._last[key] = data
                lines.append(f"{t:9.3f}  {kind.upper()}  id={mid:<3} {self._render(data)}")
        if lines:
            self.log.appendPlainText("\n".join(lines))
