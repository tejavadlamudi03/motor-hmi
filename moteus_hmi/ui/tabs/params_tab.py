"""Parameters: read / edit / save controller configuration, signed
export and import with a reviewed diff."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ...core.config_io import (COMMS_KEYS, SignatureStatus, diff_configs, is_sensitive,
                               parse_config_text, read_backup, validate_kv, write_backup)
from ...core.controllers import F_CONF, F_CONF_PERSIST
from ...security.auth import Perm
from .. import theme
from ..widgets import button, confirm, muted
from .base import HmiTab


class DiffDialog(QDialog):
    def __init__(self, rows, status: SignatureStatus, meta: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review configuration import")
        self.resize(820, 560)
        lay = QVBoxLayout(self)
        color = {"valid": theme.OK, "invalid": theme.ALARM, "unsigned": theme.WARN}[status.value]
        sig = QLabel(f"Signature: {status.value.upper()}   " +
                     "  ".join(f"{k}: {v}" for k, v in meta.items() if k in ("created", "device", "user", "profile")))
        sig.setStyleSheet(f"color: {color}; font-weight: 600;")
        lay.addWidget(sig)
        lay.addWidget(muted("Only checked rows are written. CAN id / prefix changes are unchecked by "
                            "default because they break communication immediately."))
        self.table = QTableWidget(len(rows), 4)
        self.table.setHorizontalHeaderLabels(["Apply", "Parameter", "Current", "From file"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for r, (k, cur, new) in enumerate(rows):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked if k in COMMS_KEYS else Qt.Checked)
            self.table.setItem(r, 0, chk)
            key = QTableWidgetItem(k)
            if is_sensitive(k):
                key.setForeground(QColor(theme.WARN))
            self.table.setItem(r, 1, key)
            self.table.setItem(r, 2, QTableWidgetItem("(missing)" if cur is None else cur))
            self.table.setItem(r, 3, QTableWidgetItem(new))
        lay.addWidget(self.table)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Write selected")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def selected(self) -> list[tuple[str, str]]:
        return [(self.table.item(r, 1).text(), self.table.item(r, 3).text())
                for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == Qt.Checked]


class ParamsTab(HmiTab):
    title = "Parameters"
    perm = Perm.VIEW

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.read_btn = button("Read all", "primary", self._read)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter parameters… (e.g. pid_position, max_, aux1)")
        self.search.textChanged.connect(self._filter)
        self.apply_btn = button("Apply changes", slot=self._apply, tip="conf set (RAM only)")
        self.save_btn = button("Save to flash", "danger", self._save, tip="conf write")
        self.export_btn = button("Export parameters…", slot=self._export)
        self.import_btn = button("Import parameters…", slot=self._import)
        for w in (self.read_btn, self.search, self.apply_btn, self.save_btn, self.export_btn, self.import_btn):
            bar.addWidget(w)
        lay.addLayout(bar)
        self.info = muted("Read the configuration from the selected controller. Edited values turn "
                          "blue; orange names are hardware / motor-model parameters.")
        lay.addWidget(self.info)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Parameter", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._edited)
        lay.addWidget(self.table)
        self.config: dict[str, str] = {}
        self.pending: dict[str, str] = {}
        self._loading = False
        self.on_session()

    # ------------------------------------------------------------------
    def on_session(self) -> None:
        c = self.ctx
        conn = c.connected and c.profile.has(F_CONF)
        cfg = c.can(Perm.CONFIGURE)
        self.read_btn.setEnabled(conn)
        self.apply_btn.setEnabled(conn and cfg)
        self.save_btn.setEnabled(conn and cfg and c.profile.has(F_CONF_PERSIST))
        self.export_btn.setEnabled(conn and cfg)
        self.import_btn.setEnabled(conn and cfg)
        trig = QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed if cfg else QTableWidget.NoEditTriggers
        self.table.setEditTriggers(trig)

    def on_connection(self, connected: bool) -> None:
        if not connected:
            self.table.setRowCount(0)
            self.config, self.pending = {}, {}
        self.on_session()

    def on_motor(self, motor_id: int) -> None:
        self.table.setRowCount(0)
        self.config, self.pending = {}, {}

    def _mid(self):
        return self.ctx.selected_motor

    def _read(self, then=None) -> None:
        mid = self._mid()
        if mid is None:
            return

        def ok(text):
            self.config = dict(parse_config_text(text))
            self.pending = {}
            self._populate()
            self.info.setText(f"{len(self.config)} parameters read from controller {mid} at "
                              f"{time.strftime('%H:%M:%S')}.")
            if then:
                then()

        self.info.setText("Reading configuration…")
        self.ctx.run(self.ctx.worker.diagnostic(mid, "conf enumerate", timeout_s=30.0), ok=ok,
                     what="Read configuration")

    def _populate(self) -> None:
        self._loading = True
        self.table.setRowCount(len(self.config))
        for r, (k, v) in enumerate(sorted(self.config.items())):
            key = QTableWidgetItem(k)
            key.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if is_sensitive(k):
                key.setForeground(QColor(theme.WARN))
            self.table.setItem(r, 0, key)
            self.table.setItem(r, 1, QTableWidgetItem(v))
        self._loading = False
        self._filter(self.search.text())

    def _filter(self, text: str) -> None:
        t = text.strip().lower()
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            self.table.setRowHidden(r, bool(t) and t not in item.text().lower())

    def _edited(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != 1:
            return
        key = self.table.item(item.row(), 0).text()
        value = item.text().strip()
        if value == self.config.get(key):
            self.pending.pop(key, None)
            item.setForeground(QColor(theme.TEXT))
        else:
            self.pending[key] = value
            item.setForeground(QColor(theme.ACCENT))

    def _write_items(self, items: list[tuple[str, str]], action: str) -> None:
        mid = self._mid()
        bad = [e for k, v in items if (e := validate_kv(k, v))]
        if bad:
            QMessageBox.warning(self, "Invalid values", "\n".join(bad))
            return
        if any(is_sensitive(k) for k, _ in items):
            if not confirm(self, "Hardware parameters", "You are changing hardware / motor-model "
                           "parameters:\n" + "\n".join(k for k, _ in items if is_sensitive(k)),
                           typed="APPLY"):
                return
        w = self.ctx.worker

        async def apply():
            errors = []
            for k, v in items:
                try:
                    await w._diagnostic(mid, f"conf set {k} {v}", False, 5.0)
                except Exception as e:
                    errors.append(f"{k}: {e}")
            return errors

        def done(errors):
            self.ctx.audit(action, {"motor": mid, "changes": dict(items), "errors": errors},
                           "partial" if errors else "ok")
            if errors:
                QMessageBox.warning(self, "Some values were rejected", "\n".join(errors))
            self.ctx.status_message.emit(f"Wrote {len(items) - len(errors)} parameter(s) to RAM. "
                                         "Use 'Save to flash' to make them permanent.", 8000)
            self._read()

        self.ctx.run(w.run_coroutine(apply), ok=done, what="Write parameters")

    def _apply(self) -> None:
        if not self.pending or not self.ctx.require(Perm.CONFIGURE, self):
            return
        self._write_items(list(self.pending.items()), "config.set")

    def _save(self) -> None:
        mid = self._mid()
        if mid is None or not self.ctx.require(Perm.CONFIGURE, self):
            return
        if not confirm(self, "Save to flash", f"Store the current RAM configuration of controller "
                       f"{mid} permanently?", typed="SAVE"):
            return
        self.ctx.run(self.ctx.worker.diagnostic(mid, "conf write"), what="conf write",
                     ok=lambda _: (self.ctx.audit("config.write", {"motor": mid}),
                                   self.ctx.status_message.emit("Configuration saved to flash", 5000)))

    def _export(self) -> None:
        mid = self._mid()
        if mid is None or not self.ctx.require(Perm.CONFIGURE, self):
            return

        def go():
            default = self.ctx.paths.backups / f"motor{mid}-{time.strftime('%Y%m%d-%H%M%S')}.cfg"
            path, _ = QFileDialog.getSaveFileName(self, "Export parameters", str(default), "Config (*.cfg *.txt)")
            if not path:
                return
            dev = next((d for d in self.ctx.devices if d.motor_id == mid), None)
            write_backup(Path(path), self.config, {
                "device": f"id {mid} {dev.uuid if dev else ''}".strip(),
                "profile": self.ctx.profile.key, "user": self.ctx.username}, self.ctx.keystore)
            self.ctx.audit("config.export", {"motor": mid, "file": path, "count": len(self.config)})
            self.ctx.status_message.emit(f"Exported {len(self.config)} parameters (signed) to {path}", 8000)

        self._read(then=go)

    def _import(self) -> None:
        mid = self._mid()
        if mid is None or not self.ctx.require(Perm.CONFIGURE, self):
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import parameters", str(self.ctx.paths.backups),
                                              "Config (*.cfg *.txt);;All files (*)")
        if not path:
            return
        try:
            bk = read_backup(Path(path), self.ctx.keystore)
        except OSError as e:
            QMessageBox.critical(self, "Import", str(e))
            return
        if bk.status == SignatureStatus.INVALID:
            self.ctx.audit("config.import", {"file": path}, "signature_invalid")
            if not confirm(self, "Signature does not match", "This file's signature does not match this "
                           "installation's key. Either it was changed after export, or it was exported "
                           "on another laptop / HMI installation (each has its own key). Import it only "
                           "if you know where it came from, and review every value in the next step.",
                           typed="IMPORT ANYWAY"):
                return
        elif bk.status == SignatureStatus.UNSIGNED:
            if not confirm(self, "Unsigned file", "This file has no HMI signature (for example a "
                           "moteus_tool --dump-config file). Review every value before writing."):
                return

        def show():
            rows = diff_configs(self.config, bk.config)
            if not rows:
                QMessageBox.information(self, "Import", "The controller already matches this file.")
                return
            dlg = DiffDialog(rows, bk.status, bk.meta, self)
            if dlg.exec() == QDialog.Accepted and dlg.selected():
                self._write_items(dlg.selected(), "config.import")

        self._read(then=show)
