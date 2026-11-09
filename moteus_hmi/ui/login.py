"""Login, first-run setup, recovery, password change and screen lock."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QVBoxLayout)

from .. import APP_NAME
from ..security.auth import AuthError, Session, UserStore, password_problems
from . import theme
from .widgets import button, muted


def _pw() -> QLineEdit:
    e = QLineEdit()
    e.setEchoMode(QLineEdit.Password)
    return e


class LoginDialog(QDialog):
    """Normal login, or screen-lock re-authentication (``locked_user``)."""

    def __init__(self, users: UserStore, audit, locked_user: Optional[str] = None,
                 estop: Optional[Callable[[], None]] = None, parent=None):
        super().__init__(parent)
        self.users, self.audit = users, audit
        self.session: Optional[Session] = None
        self.setWindowTitle(f"{APP_NAME} – " + ("Locked" if locked_user else "Sign in"))
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size: 18pt; font-weight: 700;")
        lay.addWidget(title)
        lay.addWidget(muted("Screen locked after inactivity. Sign in to continue (another user may "
                            "take over)." if locked_user else "Authorised personnel only. All actions "
                            "are recorded."))
        f = QFormLayout()
        self.user = QLineEdit(locked_user or "")
        self.pw = _pw()
        f.addRow("User", self.user)
        f.addRow("Password", self.pw)
        lay.addLayout(f)
        self.msg = QLabel("")
        self.msg.setStyleSheet(f"color: {theme.ALARM};")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        row = QHBoxLayout()
        if estop:
            row.addWidget(button("E-STOP", "estop", estop))
        row.addStretch(1)
        ok = button("Sign in", "primary", self._try)
        ok.setDefault(True)
        row.addWidget(ok)
        if not locked_user:
            row.addWidget(button("Quit", slot=self.reject))
        lay.addLayout(row)
        (self.pw if locked_user else self.user).setFocus()

    def _try(self) -> None:
        name = self.user.text().strip()
        try:
            u = self.users.authenticate(name, self.pw.text())
        except AuthError as e:
            self.audit.record(name or "-", "auth.login", {"reason": str(e)}, "failed")
            self.msg.setText(str(e))
            self.pw.clear()
            return
        self.audit.record(u.username, "auth.login", None, "ok", role=u.role_enum.label)
        if u.must_change:
            dlg = ChangePasswordDialog(self.users, u.username, self)
            if dlg.exec() != QDialog.Accepted:
                self.msg.setText("Password change required.")
                return
            self.audit.record(u.username, "auth.password_change", None, "ok")
        self.session = Session(user=u)
        self.accept()

    def reject(self) -> None:  # Esc must not bypass a lock screen
        if self.windowTitle().endswith("Locked"):
            return
        super().reject()


class ChangePasswordDialog(QDialog):
    def __init__(self, users: UserStore, username: str, parent=None):
        super().__init__(parent)
        self.users, self.username = users, username
        self.setWindowTitle("Change password")
        f = QFormLayout(self)
        f.addRow(muted("Choose a new password: ≥10 characters with upper case, lower case and a digit."))
        self.p1, self.p2 = _pw(), _pw()
        f.addRow("New password", self.p1)
        f.addRow("Repeat", self.p2)
        self.msg = QLabel("")
        self.msg.setStyleSheet(f"color: {theme.ALARM};")
        f.addRow(self.msg)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _ok(self) -> None:
        if self.p1.text() != self.p2.text():
            self.msg.setText("Passwords do not match")
            return
        try:
            self.users.set_password(self.username, self.p1.text(), must_change=False)
        except AuthError as e:
            self.msg.setText(str(e))
            return
        self.accept()


class SetupDialog(QDialog):
    """First start (or recovery): create the administrator account."""

    def __init__(self, users: UserStore, recovery: bool = False, parent=None):
        super().__init__(parent)
        self.users, self.recovery = users, recovery
        self.code = ""
        self.setWindowTitle(f"{APP_NAME} – " + ("Recovery" if recovery else "First-time setup"))
        self.setMinimumWidth(460)
        f = QFormLayout(self)
        f.addRow(QLabel("<b>" + ("Integrity problem detected" if recovery else "Create the administrator account") + "</b>"))
        f.addRow(muted("The user database is missing or was modified outside the HMI. Enter the "
                       "recovery code that was shown at installation to create a new administrator."
                       if recovery else
                       "There are no default accounts. The administrator manages users, roles and "
                       "the audit trail."))
        self.rc = QLineEdit()
        if recovery:
            f.addRow("Recovery code", self.rc)
        self.name = QLineEdit("admin")
        self.p1, self.p2 = _pw(), _pw()
        f.addRow("Admin user name", self.name)
        f.addRow("Password", self.p1)
        f.addRow("Repeat", self.p2)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet(f"color: {theme.ALARM};")
        f.addRow(self.msg)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _ok(self) -> None:
        if self.p1.text() != self.p2.text():
            self.msg.setText("Passwords do not match")
            return
        probs = password_problems(self.p1.text(), self.name.text())
        if probs:
            self.msg.setText("Password needs " + ", ".join(probs))
            return
        try:
            if self.recovery:
                self.code = self.users.recover(self.rc.text(), self.name.text().strip(), self.p1.text())
            else:
                self.code = self.users.setup_admin(self.name.text().strip(), self.p1.text())
        except AuthError as e:
            self.msg.setText(str(e))
            return
        box = QMessageBox(self)
        box.setWindowTitle("Recovery code – write it down")
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.setText("Store this one-time recovery code somewhere safe (it is shown only once). It "
                    f"is needed if the user database is ever lost or tampered with:\n\n{self.code}")
        box.exec()
        self.accept()
