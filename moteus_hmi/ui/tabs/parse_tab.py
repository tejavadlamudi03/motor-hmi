"""Parse: decode raw frames and inspect recorded data."""

from __future__ import annotations

import csv
import hashlib
import math
import statistics
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtWidgets import (QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView,
                               QLineEdit, QPlainTextEdit, QTableWidget, QTableWidgetItem, QVBoxLayout)

from .. import theme
from ..widgets import button, group, muted
from .base import HmiTab


def decode_moteus(data: bytes) -> str:
    import moteus
    from moteus import Register
    parsed = moteus.parse_registers(data)

    def name(reg: int) -> str:
        try:
            return Register(reg).name
        except ValueError:
            return f"0x{reg:03x}"

    out = []
    if parsed.command:
        out.append("WRITE (command):")
        for reg, v in parsed.command.items():
            out.append(f"  {name(reg):<32} 0x{reg:03x} = {v}")
    if parsed.response:
        out.append("REPLY (telemetry):")
        for reg, v in parsed.response.items():
            out.append(f"  {name(reg):<32} 0x{reg:03x} = {v}")
    if parsed.query:
        res = {0: "int8", 1: "int16", 2: "int32", 3: "float"}
        out.append("READ request:")
        for reg, r in parsed.query:
            out.append(f"  {name(reg):<32} 0x{reg:03x} as {res.get(r, r)}")
    if parsed.errors:
        out.append(f"ERRORS: {parsed.errors}")
    return "\n".join(out) or "(no registers decoded)"


def decode_fcp(data: bytes) -> str:
    from ...core.st_mcsdk_backend import (FC_ACK, FC_NACK, NACK_ERRORS, REGISTERS, fcp_crc)
    if len(data) < 3:
        return "Frame too short"
    head, n = data[0], data[1]
    body, crc = data[2:2 + n], data[2 + n] if len(data) > 2 + n else None
    out = [f"header 0x{head:02x}  (motor {head >> 5}, code 0x{head & 0x1f:02x})",
           f"payload length {n}: {body.hex(' ')}",
           f"crc 0x{crc:02x} – {'OK' if crc == fcp_crc(data[:2 + n]) else 'MISMATCH'}" if crc is not None else "crc missing"]
    codes = {1: "SET_REG", 2: "GET_REG", 3: "EXECUTE_CMD", 6: "GET_BOARD_INFO", 7: "SET_RAMP",
             10: "SET_CURRENT_REF"}
    if head == FC_ACK:
        out.append("→ ACK reply")
    elif head == FC_NACK:
        out.append(f"→ NACK: {NACK_ERRORS.get(body[0] if body else 0, 'unknown')}")
    else:
        out.append(f"→ {codes.get(head & 0x1f, 'unknown frame code')}")
        if head & 0x1f in (1, 2) and body:
            reg = {v[0]: k for k, v in REGISTERS.items()}.get(body[0], "?")
            out.append(f"   register 0x{body[0]:02x} = {reg}")
    return "\n".join(out)


class ParseTab(HmiTab):
    title = "Parse"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QGridLayout(self)
        d = QVBoxLayout()
        row = QHBoxLayout()
        self.proto = QComboBox()
        self.proto.addItem("moteus CAN-FD register frame", "moteus")
        self.proto.addItem("ST MCSDK FCP frame", "fcp")
        self.hex = QLineEdit()
        self.hex.setPlaceholderText("Paste frame bytes, e.g. 01 00 0A 0C 06 20 00 00 80 3F …")
        row.addWidget(self.proto)
        row.addWidget(self.hex, 1)
        row.addWidget(button("Decode", "primary", self._decode))
        row.addWidget(button("Use last TX frame", slot=self._use_last))
        d.addLayout(row)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setStyleSheet("font-family: monospace;")
        d.addWidget(self.out)
        lay.addWidget(group("Frame decoder", d), 0, 0)

        r = QVBoxLayout()
        rr = QHBoxLayout()
        rr.addWidget(button("Open recording…", slot=self._open))
        self.col = QComboBox()
        self.col.currentTextChanged.connect(self._plot_col)
        rr.addWidget(self.col, 1)
        self.integrity = muted("", wrap=False)
        rr.addWidget(self.integrity)
        r.addLayout(rr)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.curve = self.plot.plot(pen=pg.mkPen(theme.TRACE["filtered"], width=1))
        r.addWidget(self.plot, 2)
        self.stats = QTableWidget(0, 6)
        self.stats.setHorizontalHeaderLabels(["Column", "Min", "Max", "Mean", "Std", "Samples"])
        self.stats.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        r.addWidget(self.stats, 1)
        lay.addWidget(group("Recording viewer (CSV from Feedback & Filters)", r), 0, 1)
        lay.setColumnStretch(1, 2)
        self._data: dict[str, list[float]] = {}
        self._last_tx = ""

    def on_batch(self, batch) -> None:
        mid = self.ctx.selected_motor
        self._last_tx = batch.bus.get("last_tx", {}).get(mid, self._last_tx)

    def _use_last(self) -> None:
        if self._last_tx:
            self.hex.setText(self._last_tx)
            self._decode()

    def _decode(self) -> None:
        try:
            data = bytes.fromhex(self.hex.text().replace(",", " ").replace("0x", ""))
        except ValueError as e:
            self.out.setPlainText(f"Invalid hex: {e}")
            return
        try:
            self.out.setPlainText(decode_moteus(data) if self.proto.currentData() == "moteus" else decode_fcp(data))
        except Exception as e:
            self.out.setPlainText(f"Could not decode: {e}")

    def _open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open recording", str(self.ctx.paths.recordings), "CSV (*.csv)")
        if not path:
            return
        side = Path(path + ".sha256")
        if side.exists():
            expect = side.read_text().split()[0]
            actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            ok = expect == actual
            self.integrity.setText("✔ SHA-256 verified" if ok else "✘ file modified after recording")
            self.integrity.setStyleSheet(f"color: {theme.OK if ok else theme.ALARM};")
        else:
            self.integrity.setText("no checksum")
        with open(path, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.reader(l for l in fh if not l.startswith("#"))]
        if not rows:
            return
        header, body = rows[0], rows[1:]
        self._data = {h: [] for h in header}
        for row in body:
            for h, v in zip(header, row):
                try:
                    self._data[h].append(float(v) if v != "" else math.nan)
                except ValueError:
                    self._data[h].append(math.nan)
        numeric = [h for h in header if h not in ("t_s",)]
        self.col.blockSignals(True)
        self.col.clear()
        self.col.addItems(numeric)
        self.col.blockSignals(False)
        self.stats.setRowCount(len(numeric))
        for r, h in enumerate(numeric):
            v = [x for x in self._data[h] if math.isfinite(x)]
            vals = [h] + ([f"{min(v):.5g}", f"{max(v):.5g}", f"{statistics.fmean(v):.5g}",
                           f"{statistics.pstdev(v):.5g}", str(len(v))] if v else ["—"] * 4 + ["0"])
            for c, x in enumerate(vals):
                self.stats.setItem(r, c, QTableWidgetItem(x))
        self.ctx.audit("data.open_recording", {"file": path})
        if "position" in numeric:
            self.col.setCurrentText("position")
        self._plot_col(self.col.currentText())

    def _plot_col(self, name: str) -> None:
        if name in self._data and "t_s" in self._data:
            self.curve.setData(self._data["t_s"], self._data[name], connect="finite")
            self.plot.setTitle(name)
