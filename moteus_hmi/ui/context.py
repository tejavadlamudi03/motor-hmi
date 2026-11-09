"""Shared application context handed to every tab."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from ..core.bus_worker import BusWorker, MotorStatus, TelemetryBatch
from ..core.controllers import ControllerProfile, get_profile
from ..core.types import DeviceDescriptor
from ..paths import AppPaths
from ..security.audit import AuditLog
from ..security.auth import Perm, Session, UserStore
from ..security.keystore import KeyStore
from ..settings import AppSettings, SettingsStore

log = logging.getLogger(__name__)


class AppContext(QObject):
    motor_selected = Signal(int)
    session_changed = Signal()
    units_changed = Signal(str)
    status_message = Signal(str, int)

    def __init__(self, paths: AppPaths, keystore: KeyStore, users: UserStore, audit_log: AuditLog,
                 settings_store: SettingsStore, settings: AppSettings, worker: BusWorker):
        super().__init__()
        self.paths = paths
        self.keystore = keystore
        self.users = users
        self.audit_log = audit_log
        self.settings_store = settings_store
        self.settings = settings
        self.worker = worker
        self.session: Optional[Session] = None
        self.selected_motor: Optional[int] = None
        self.devices: list[DeviceDescriptor] = []
        self.status: dict[int, MotorStatus] = {}
        self.connected = False
        self._callbacks: dict[int, tuple[Optional[Callable], Optional[Callable]]] = {}
        worker.request_done.connect(self._dispatch)
        worker.audit_hook = self._worker_audit

    # ---- identity / permissions ---------------------------------------
    @property
    def username(self) -> str:
        return self.session.username if self.session else "-"

    def can(self, perm: Perm) -> bool:
        return bool(self.session and self.session.can(perm))

    def require(self, perm: Perm, parent: Optional[QWidget] = None) -> bool:
        if self.can(perm):
            return True
        QMessageBox.warning(parent, "Not permitted",
                            f"Your role ({self.session.role.label if self.session else 'none'}) "
                            f"does not allow '{perm.value}'.")
        self.audit("access.denied", {"perm": perm.value}, "denied")
        return False

    def audit(self, action: str, details: Any = None, outcome: str = "ok") -> None:
        try:
            self.audit_log.record(self.username, action, details, outcome,
                                  role=self.session.role.label if self.session else "")
        except OSError as e:  # never crash the HMI on a logging problem
            log.error("audit write failed: %s", e)

    def _worker_audit(self, action: str, details: Any, outcome: str) -> None:
        # Called on the worker thread; AuditLog is thread-safe.
        self.audit(action, details, outcome)

    # ---- controller -----------------------------------------------------
    @property
    def profile(self) -> ControllerProfile:
        return self.worker.profile if self.connected else get_profile(self.settings.transport.get("profile"))

    @property
    def units(self) -> str:
        return self.settings.display_units

    def set_units(self, units: str) -> None:
        self.settings.display_units = units
        self.save_settings()
        self.units_changed.emit(units)

    def select_motor(self, motor_id: Optional[int]) -> None:
        if motor_id != self.selected_motor:
            self.selected_motor = motor_id
            if motor_id is not None:
                self.motor_selected.emit(motor_id)

    def save_settings(self) -> None:
        try:
            self.settings_store.save(self.settings)
        except OSError as e:
            log.error("settings save failed: %s", e)

    # ---- async requests ------------------------------------------------
    def run(self, rid: int, ok: Optional[Callable[[Any], None]] = None,
            err: Optional[Callable[[str], None]] = None, what: str = "") -> int:
        def default_err(msg: str) -> None:
            self.status_message.emit(f"{what or 'Request'} failed: {msg}", 8000)
        self._callbacks[rid] = (ok, err or default_err)
        return rid

    def _dispatch(self, rid: int, ok: bool, result: Any) -> None:
        cb = self._callbacks.pop(rid, None)
        if not cb:
            if not ok:
                self.status_message.emit(f"Error: {result}", 8000)
            return
        on_ok, on_err = cb
        try:
            if ok and on_ok:
                on_ok(result)
            elif not ok and on_err:
                on_err(str(result))
        except Exception:
            log.exception("callback failed")

    def current_sample(self, motor_id: Optional[int] = None):
        mid = motor_id if motor_id is not None else self.selected_motor
        st = self.status.get(mid) if mid is not None else None
        return st.sample if st else None
