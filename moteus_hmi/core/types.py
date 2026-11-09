"""Plain data types shared by the backends, the control loop and the UI.

Units follow the moteus conventions: revolutions, revolutions/s, N*m,
Amps, Volts, degrees Celsius, seconds.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field, asdict, fields
from typing import Any, Optional

NAN = math.nan


def finite(x: Optional[float]) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


class ControlMode(str, enum.Enum):
    """Operator-level control modes.  Mapped onto moteus firmware modes
    by the backend."""

    QUERY = "query"            # no command, telemetry only (disarmed)
    STOP = "stop"              # torque off, clears faults
    POSITION = "position"      # trajectory-limited position control
    VELOCITY = "velocity"      # position = NaN, constant velocity
    TORQUE = "torque"          # kp=kd=0, feed-forward torque only
    HOLD = "hold"              # firmware zero-velocity (damping) mode
    BRAKE = "brake"            # phases shorted, passive braking
    STAY_WITHIN = "stay_within"  # hold inside [lower, upper]

    @property
    def label(self) -> str:
        return {
            "query": "Monitor only",
            "stop": "Stop (torque off)",
            "position": "Position",
            "velocity": "Velocity",
            "torque": "Torque",
            "hold": "Zero-velocity hold",
            "brake": "Brake",
            "stay_within": "Stay within bounds",
        }[self.value]


MOTION_MODES = (ControlMode.POSITION, ControlMode.VELOCITY,
                ControlMode.TORQUE, ControlMode.STAY_WITHIN)


class FirmwareMode(enum.IntEnum):
    """Values of moteus register 0x000 (mirrors moteus.Mode)."""

    STOPPED = 0
    FAULT = 1
    ENABLING = 2
    CALIBRATING = 3
    CALIBRATION_COMPLETE = 4
    PWM = 5
    VOLTAGE = 6
    VOLTAGE_FOC = 7
    VOLTAGE_DQ = 8
    CURRENT = 9
    POSITION = 10
    TIMEOUT = 11
    ZERO_VELOCITY = 12
    STAY_WITHIN = 13
    MEASURE_IND = 14
    BRAKE = 15

    @classmethod
    def name_of(cls, value: int) -> str:
        try:
            return cls(int(value)).name.replace("_", " ").title()
        except (ValueError, TypeError):
            return f"Mode {value}"


HOME_STATE_NAMES = {0: "Relative", 1: "Rotor", 2: "Output"}


@dataclass
class MotorCommand:
    mode: ControlMode = ControlMode.QUERY
    position: float = NAN
    velocity: float = 0.0
    feedforward_torque: float = 0.0
    kp_scale: float = 1.0
    kd_scale: float = 1.0
    maximum_torque: float = NAN
    velocity_limit: float = NAN
    accel_limit: float = NAN
    lower_bound: float = NAN
    upper_bound: float = NAN
    # Filled in by the supervisor right before transmission.
    watchdog_timeout: float = NAN

    def copy(self, **changes: Any) -> "MotorCommand":
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(changes)
        return MotorCommand(**data)

    def describe(self) -> str:
        m = self.mode
        if m == ControlMode.POSITION:
            return (f"position {self.position:+.4f} rev, v_lim {self.velocity_limit:.3f}, "
                    f"a_lim {self.accel_limit:.3f}, T_max {self.maximum_torque:.3f}")
        if m == ControlMode.VELOCITY:
            return f"velocity {self.velocity:+.4f} rev/s, a_lim {self.accel_limit:.3f}"
        if m == ControlMode.TORQUE:
            return f"torque {self.feedforward_torque:+.4f} Nm"
        if m == ControlMode.STAY_WITHIN:
            return f"stay within [{self.lower_bound:+.4f}, {self.upper_bound:+.4f}] rev"
        return m.label


@dataclass
class Telemetry:
    motor_id: int
    timestamp: float = 0.0          # time.monotonic() at receipt
    mode: int = 0
    position: float = NAN
    velocity: float = NAN
    torque: float = NAN
    q_current: float = NAN
    d_current: float = NAN
    power: float = NAN
    motor_temperature: float = NAN
    temperature: float = NAN
    voltage: float = NAN
    fault: int = 0
    trajectory_complete: bool = False
    home_state: int = 0
    control_position: float = NAN
    control_velocity: float = NAN
    # Vendor specific human readable state / fault (empty -> derive from
    # the moteus mode / fault codes).
    state_text: str = ""
    fault_text: str = ""

    @property
    def position_error(self) -> float:
        if finite(self.control_position) and finite(self.position):
            return self.position - self.control_position
        return NAN

    @property
    def velocity_error(self) -> float:
        if finite(self.control_velocity) and finite(self.velocity):
            return self.velocity - self.control_velocity
        return NAN

    def value(self, name: str) -> float:
        return float(getattr(self, name))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["position_error"] = self.position_error
        d["velocity_error"] = self.velocity_error
        return d


# Continuous signals that can be plotted / filtered / logged.
SIGNALS: dict[str, tuple[str, str]] = {
    "position": ("Position", "rev"),
    "velocity": ("Velocity", "rev/s"),
    "torque": ("Torque", "N·m"),
    "q_current": ("Q current", "A"),
    "d_current": ("D current", "A"),
    "power": ("Power", "W"),
    "voltage": ("Bus voltage", "V"),
    "temperature": ("Board temp", "°C"),
    "motor_temperature": ("Motor temp", "°C"),
    "position_error": ("Following error", "rev"),
    "velocity_error": ("Velocity error", "rev/s"),
}


@dataclass
class DeviceDescriptor:
    motor_id: int
    uuid: str = ""
    bus: str = ""
    address: Any = None      # backend specific addressing object
    responding: bool = True

    def label(self) -> str:
        return f"ID {self.motor_id}" + (f" ({self.bus})" if self.bus else "")


TRANSPORT_KINDS = {
    "auto": "USB-CAN-FD auto-detect (fdcanusb, then python-can)",
    "fdcanusb": "mjbots fdcanusb / mjcanfd-usb (USB serial)",
    "socketcan": "SocketCAN CAN-FD (Linux, e.g. can0)",
    "pythoncan": "python-can CAN-FD (PCAN, Kvaser, Vector, IXXAT …)",
    "st_serial": "USB serial / ST-LINK virtual COM port",
    "virtual_canfd": "Motor Simulator app (virtual CAN-FD bus)",
}


@dataclass
class TransportConfig:
    profile: str = "moteus_r4_11"
    kind: str = "auto"
    fdcanusb_path: str = ""
    can_interface: str = "pcan"
    can_channel: str = "can0"
    disable_brs: bool = False
    can_prefix: int = 0
    explicit_ids: list[int] = field(default_factory=list)
    virtual_channel: str = "239.74.163.2"   # udp_multicast group of the Motor Simulator
    virtual_port: int = 43113               # UDP port = bus identity (one bus per port)
    # Serial-link controllers (ST MCSDK)
    serial_port: str = ""
    baudrate: int = 115200
    amps_per_digit: float = 0.0          # 0 -> profile default
    torque_constant: float = 0.0         # N*m/A, 0 -> profile default

    def describe(self) -> str:
        if self.kind == "fdcanusb":
            return f"fdcanusb {self.fdcanusb_path or '(auto)'}"
        if self.kind == "socketcan":
            return f"socketcan {self.can_channel}"
        if self.kind == "pythoncan":
            return f"python-can {self.can_interface}:{self.can_channel}"
        if self.kind == "st_serial":
            return f"serial {self.serial_port or '(none)'} @ {self.baudrate}"
        if self.kind == "virtual_canfd":
            return f"virtual CAN-FD {self.virtual_channel}:{self.virtual_port}"
        return "USB-CAN auto-detect"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TransportConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})
