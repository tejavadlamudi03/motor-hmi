from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QWidget

from ...security.auth import Perm
from ..context import AppContext


class HmiTab(QWidget):
    """Base class: every tab receives the same lifecycle callbacks."""

    title = ""
    perm: Perm = Perm.VIEW          # permission needed to see the tab

    def __init__(self, ctx: AppContext, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ctx = ctx

    def on_batch(self, batch) -> None: ...
    def on_connection(self, connected: bool) -> None: ...
    def on_devices(self, devices) -> None: ...
    def on_motor(self, motor_id: int) -> None: ...
    def on_session(self) -> None: ...
    def on_alarm(self, alarm) -> None: ...
    def on_noise(self, motor_id: int, reports) -> None: ...
    def on_units(self, units: str) -> None: ...
