"""Users & Audit (administrators)."""

from __future__ import annotations

import json
import shutil
import time

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
                               QLineEdit, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ...security.auth import AuthError, Perm, Role
from .. import theme
from ..widgets import button, confirm, group, muted
from .base import HmiTab


class AddUserDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add user")
        f = QFormLayout(self)
        self.name = QLineEdit()
        self.role = QComboBox()
        for r in Role:
            self.role.addItem(r.label, int(r))
        self.role.setCurrentIndex(1)
        self.pw = QLineEdit(); self.pw.setEchoMode(QLineEdit.Password)
        self.pw2 = QLineEdit(); self.pw2.setEchoMode(QLineEdit.Password)
        f.addRow("User name", self.name)
        f.addRow("Role", self.role)
        f.addRow("Temporary password", self.pw)
        f.addRow("Repeat", self.pw2)
        f.addRow(muted("The user must change the password at first login."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


class AdminTab(HmiTab):
    title = "Users && Audit"
    perm = Perm.MANAGE_USERS

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        lay = QGridLayout(self)
        u = QVBoxLayout()
        self.users = QTableWidget(0, 6)
        self.users.setHorizontalHeaderLabels(["User", "Role", "Last login", "Locked", "Disabled", "Must change pw"])
        self.users.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.users.setEditTriggers(QTableWidget.NoEditTriggers)
        self.users.setSelectionBehavior(QTableWidget.SelectRows)
        u.addWidget(self.users)
        row = QHBoxLayout()
        for text, slot in (("Add…", self._add), ("Reset password…", self._reset_pw), ("Change role…", self._role),
                           ("Unlock", self._unlock), ("Enable / disable", self._toggle), ("Delete", self._delete)):
            row.addWidget(button(text, slot=slot))
        u.addLayout(row)
        st = QHBoxLayout()
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(1, 240)
        self.timeout.setSuffix(" min")
        self.timeout.setValue(ctx.settings.session_timeout_min)
        st.addWidget(muted("Auto-lock after inactivity", wrap=False))
        st.addWidget(self.timeout)
        st.addWidget(button("Save", slot=self._save_timeout))
        st.addStretch(1)
        u.addLayout(st)
        lay.addWidget(group("Users and roles", u), 0, 0)

        a = QVBoxLayout()
        ar = QHBoxLayout()
        ar.addWidget(button("Refresh", slot=self._load_audit))
        ar.addWidget(button("Verify integrity", "primary", self._verify))
        ar.addWidget(button("Export…", slot=self._export))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter by user / action…")
        self.filter.textChanged.connect(self._load_audit)
        ar.addWidget(self.filter, 1)
        a.addLayout(ar)
        self.verify_lbl = muted("", wrap=False)
        a.addWidget(self.verify_lbl)
        self.audit_tbl = QTableWidget(0, 6)
        self.audit_tbl.setHorizontalHeaderLabels(["#", "Time (UTC)", "User", "Action", "Outcome", "Details"])
        self.audit_tbl.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.audit_tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        a.addWidget(self.audit_tbl)
        lay.addWidget(group("Audit trail (tamper-evident, HMAC chained)", a), 1, 0)
        lay.setRowStretch(1, 2)
        self.on_session()

    def on_session(self) -> None:
        if self.ctx.can(Perm.MANAGE_USERS):
            self._load_users()
            self._load_audit()

    def _selected(self):
        rows = self.users.selectionModel().selectedRows()
        return self.users.item(rows[0].row(), 0).text() if rows else None

    def _load_users(self) -> None:
        us = list(self.ctx.users.users.values())
        self.users.setRowCount(len(us))
        now = time.time()
        for r, u in enumerate(us):
            vals = [u.username, Role(u.role).label,
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(u.last_login)) if u.last_login else "never",
                    "yes" if u.locked_until > now else "", "yes" if u.disabled else "",
                    "yes" if u.must_change else ""]
            for c, v in enumerate(vals):
                self.users.setItem(r, c, QTableWidgetItem(v))

    def _do(self, action: str, fn, details: dict) -> None:
        if not self.ctx.require(Perm.MANAGE_USERS, self):
            return
        try:
            fn()
            self.ctx.audit(action, details)
        except (AuthError, KeyError) as e:
            QMessageBox.warning(self, "Users", str(e))
            self.ctx.audit(action, {**details, "error": str(e)}, "failed")
        self._load_users()

    def _add(self) -> None:
        dlg = AddUserDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.pw.text() != dlg.pw2.text():
            QMessageBox.warning(self, "Users", "Passwords do not match")
            return
        name, role = dlg.name.text().strip(), Role(dlg.role.currentData())
        self._do("user.add", lambda: self.ctx.users.add_user(name, dlg.pw.text(), role, True),
                 {"user": name, "role": role.label})

    def _reset_pw(self) -> None:
        name = self._selected()
        if not name:
            return
        pw, ok = QInputDialog.getText(self, "Reset password", f"Temporary password for {name}:", QLineEdit.Password)
        if ok and pw:
            self._do("user.reset_password", lambda: self.ctx.users.set_password(name, pw, must_change=True),
                     {"user": name})

    def _role(self) -> None:
        name = self._selected()
        if not name:
            return
        roles = [r.label for r in Role]
        r, ok = QInputDialog.getItem(self, "Role", f"Role for {name}:", roles, 1, False)
        if ok:
            role = Role[r.upper()]
            self._do("user.role", lambda: self.ctx.users.set_role(name, role), {"user": name, "role": r})

    def _unlock(self) -> None:
        name = self._selected()
        if name:
            self._do("user.unlock", lambda: self.ctx.users.unlock(name), {"user": name})

    def _toggle(self) -> None:
        name = self._selected()
        if not name:
            return
        if name == self.ctx.username:
            QMessageBox.warning(self, "Users", "You cannot disable your own account.")
            return
        dis = not self.ctx.users.users[name].disabled
        self._do("user.disable" if dis else "user.enable",
                 lambda: self.ctx.users.set_disabled(name, dis), {"user": name})

    def _delete(self) -> None:
        name = self._selected()
        if not name or name == self.ctx.username:
            return
        if confirm(self, "Delete user", f"Delete user {name}?"):
            self._do("user.delete", lambda: self.ctx.users.delete_user(name), {"user": name})

    def _save_timeout(self) -> None:
        self.ctx.settings.session_timeout_min = self.timeout.value()
        self.ctx.save_settings()
        self.ctx.audit("settings.session_timeout", {"minutes": self.timeout.value()})

    def _load_audit(self) -> None:
        f = self.filter.text().strip().lower()
        rows = [e for e in reversed(self.ctx.audit_log.entries(2000))
                if not f or f in json.dumps(e).lower()][:1000]
        self.audit_tbl.setRowCount(len(rows))
        for r, e in enumerate(rows):
            vals = [str(e.get("seq")), str(e.get("ts", ""))[:23], e.get("user", ""), e.get("action", ""),
                    e.get("outcome", ""), json.dumps(e.get("details", ""), default=str)[:400]]
            for c, v in enumerate(vals):
                self.audit_tbl.setItem(r, c, QTableWidgetItem(v))

    def _verify(self) -> None:
        res = self.ctx.audit_log.verify()
        self.verify_lbl.setText(("✔ " if res.ok else "✘ ") + res.message)
        self.verify_lbl.setStyleSheet(f"color: {theme.OK if res.ok else theme.ALARM}; font-weight: 600;")
        self.ctx.audit("audit.verify", {"result": res.message}, "ok" if res.ok else "integrity_failure")

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export audit trail",
                                              f"audit-{time.strftime('%Y%m%d-%H%M%S')}.jsonl", "JSON lines (*.jsonl)")
        if not path:
            return
        shutil.copyfile(self.ctx.audit_log.path, path)
        res = self.ctx.audit_log.verify()
        with open(path + ".verify.txt", "w", encoding="utf-8") as fh:
            fh.write(f"exported {time.strftime('%Y-%m-%d %H:%M:%S %z')} by {self.ctx.username}\n{res.message}\n")
        self.ctx.audit("audit.export", {"file": path})
