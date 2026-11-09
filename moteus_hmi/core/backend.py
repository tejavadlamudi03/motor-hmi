"""Backend interface: everything the control loop needs from a bus.

Implementations:

* ``MoteusBackend``   - moteus controllers through the official ``moteus``
                        python library (fdcanusb, SocketCAN, python-can, and
                        the Motor Simulator app on a virtual CAN-FD bus).
* ``StMcsdkBackend``  - ST Motor Control SDK 5.x boards over USB serial.

All methods are coroutines executed on the bus worker's asyncio loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from .types import DeviceDescriptor, MotorCommand, Telemetry, TransportConfig


class BackendError(RuntimeError):
    pass


class DiagnosticError(BackendError):
    """The controller answered a diagnostic command with ERR."""


class MotorBackend(ABC):

    def __init__(self, config: TransportConfig):
        self.config = config
        self.devices: dict[int, DeviceDescriptor] = {}

    @abstractmethod
    async def open(self) -> str:
        """Open the bus.  Returns a human readable description."""

    @abstractmethod
    async def discover(self) -> list[DeviceDescriptor]:
        ...

    @abstractmethod
    def attach(self, devices: list[DeviceDescriptor]) -> None:
        """Prepare per-motor controller objects for these devices."""

    @abstractmethod
    async def cycle(self, commands: dict[int, MotorCommand],
                    timeout_s: float) -> dict[int, Optional[Telemetry]]:
        """Send one command (or query) to every attached motor and return
        the latest telemetry per motor (None = no reply this cycle)."""

    @abstractmethod
    async def stop_all(self) -> None:
        ...

    @abstractmethod
    async def diagnostic(self, motor_id: int, command: str,
                         allow_any_response: bool = False,
                         timeout_s: float = 5.0) -> str:
        """Run a text command on the diagnostic channel
        (``conf get``/``conf set``/``conf enumerate``/``conf write``...)."""

    async def diagnostic_write_only(self, motor_id: int, command: str) -> None:
        """Send a diagnostic line without waiting for the reply (needed for
        commands after which the node no longer answers on its old id)."""
        try:
            await self.diagnostic(motor_id, command, True, 1.0)
        except Exception:
            pass

    @abstractmethod
    async def read_channel(self, motor_id: int, name: str,
                           timeout_s: float = 5.0) -> dict[str, Any]:
        """Read a binary telemetry channel (servo_stats, drv8323, firmware...)."""

    @abstractmethod
    async def set_output(self, motor_id: int, position: float, exact: bool) -> None:
        """Re-reference the output position (not persisted)."""

    @abstractmethod
    async def close(self) -> None:
        ...

    def moteus_tool_args(self) -> list[str]:
        """Transport arguments for an out-of-process ``moteus_tool``."""
        return []
