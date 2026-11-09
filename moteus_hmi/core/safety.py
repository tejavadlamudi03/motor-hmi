"""Software safety supervisor.

IMPORTANT: this is a *functional* safety layer in application software.
It is not a safety-rated (SIL / PL) function.  A real installation must
also have a hard-wired emergency stop that removes motor power through a
safety relay / STO circuit, independent of this PC.  See docs/SAFETY.md.

Layers implemented here (outermost first):

1. Command clamping - every operator command is clamped against the
   per-motor ``SafetyLimits`` before it is encoded.  Trajectories are
   always sent with finite velocity / acceleration / torque limits.
2. Firmware watchdog - every motion command carries
   ``watchdog_timeout``; if the HMI, USB cable or PC dies, the moteus
   firmware itself enters its timeout mode.
3. Telemetry supervision - each cycle the measured state is checked
   (fault mode, comm loss, voltage, temperature, over-speed, soft
   position limits, following error).  Violations raise alarms and
   *trip* the motor into a safe command until an operator resets it.
4. Emergency stop - latched, overrides everything, needs a deliberate
   reset.
"""

from __future__ import annotations

import enum
import math
import time
from dataclasses import dataclass, field, fields, asdict
from typing import Optional

from .faults import fault_title, is_limit_code
from .types import ControlMode, FirmwareMode, MotorCommand, Telemetry, finite, NAN


@dataclass
class SafetyLimits:
    max_velocity: float = 2.0          # rev/s   (|v| commanded and monitored)
    max_accel: float = 10.0            # rev/s^2 (trajectory accel limit)
    max_torque: float = 0.5            # N*m     (maximum_torque / ff torque)
    position_min: float = NAN          # rev     (soft limit, NaN = none)
    position_max: float = NAN          # rev
    min_voltage: float = 10.0          # V
    max_voltage: float = 46.0          # V
    max_board_temp: float = 75.0       # degC
    max_motor_temp: float = 90.0       # degC (only if sensed)
    max_following_error: float = NAN   # rev  (NaN = not monitored)
    overspeed_factor: float = 1.25     # trip when |v| > factor * max_velocity
    kp_scale_max: float = 1.0
    kd_scale_max: float = 1.0

    def to_dict(self) -> dict:
        return {k: (None if isinstance(v, float) and math.isnan(v) else v)
                for k, v in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: dict | None) -> "SafetyLimits":
        out = cls()
        for f in fields(cls):
            if data and f.name in data:
                v = data[f.name]
                setattr(out, f.name, NAN if v is None else float(v))
        return out


# Absolute ceilings that no user (not even an admin) can exceed from the
# HMI.  Adjust for the hardware at commissioning time, in code review.
HARD_CEILINGS = SafetyLimits(
    max_velocity=50.0, max_accel=500.0, max_torque=30.0,
    min_voltage=8.0, max_voltage=54.0, max_board_temp=90.0,
    max_motor_temp=130.0, overspeed_factor=2.0,
    kp_scale_max=2.0, kd_scale_max=2.0,
)


def validate_limits(lim: SafetyLimits, ceilings: SafetyLimits = HARD_CEILINGS) -> list[str]:
    errs: list[str] = []

    def rng(name: str, lo: float, hi: float) -> None:
        v = getattr(lim, name)
        if not finite(v) or v < lo or v > hi:
            errs.append(f"{name} must be within [{lo}, {hi}] (got {v})")

    rng("max_velocity", 0.0, ceilings.max_velocity)
    rng("max_accel", 0.0, ceilings.max_accel)
    rng("max_torque", 0.0, ceilings.max_torque)
    rng("min_voltage", ceilings.min_voltage, ceilings.max_voltage)
    rng("max_voltage", ceilings.min_voltage, ceilings.max_voltage)
    rng("max_board_temp", 0.0, ceilings.max_board_temp)
    rng("max_motor_temp", 0.0, ceilings.max_motor_temp)
    rng("overspeed_factor", 1.0, ceilings.overspeed_factor)
    rng("kp_scale_max", 0.0, ceilings.kp_scale_max)
    rng("kd_scale_max", 0.0, ceilings.kd_scale_max)
    if lim.min_voltage >= lim.max_voltage:
        errs.append("min_voltage must be below max_voltage")
    if finite(lim.position_min) and finite(lim.position_max) and lim.position_min >= lim.position_max:
        errs.append("position_min must be below position_max")
    if finite(lim.max_following_error) and lim.max_following_error <= 0:
        errs.append("max_following_error must be > 0 or empty")
    return errs


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _limit(requested: float, ceiling: float) -> float:
    """Use the requested limit if finite and below the ceiling."""
    if finite(requested) and requested > 0:
        return min(requested, ceiling)
    return ceiling


def clamp_command(cmd: MotorCommand, lim: SafetyLimits) -> tuple[MotorCommand, list[str]]:
    """Return a command that respects ``lim`` plus human readable notes
    describing every modification."""
    notes: list[str] = []
    c = cmd.copy()
    pmin = lim.position_min if finite(lim.position_min) else -math.inf
    pmax = lim.position_max if finite(lim.position_max) else math.inf

    def clamp_field(name: str, lo: float, hi: float) -> None:
        v = getattr(c, name)
        if not finite(v):
            return
        nv = _clamp(v, lo, hi)
        if nv != v:
            notes.append(f"{name} {v:+.4f} clamped to {nv:+.4f}")
            setattr(c, name, nv)

    if c.mode in (ControlMode.QUERY, ControlMode.STOP, ControlMode.BRAKE):
        return c, notes

    if c.mode == ControlMode.HOLD:
        c.kd_scale = _clamp(c.kd_scale if finite(c.kd_scale) else 1.0, 0.0, lim.kd_scale_max)
        return c, notes

    c.kp_scale = _clamp(c.kp_scale if finite(c.kp_scale) else 1.0, 0.0, lim.kp_scale_max)
    c.kd_scale = _clamp(c.kd_scale if finite(c.kd_scale) else 1.0, 0.0, lim.kd_scale_max)
    c.maximum_torque = _limit(c.maximum_torque, lim.max_torque)
    c.feedforward_torque = c.feedforward_torque if finite(c.feedforward_torque) else 0.0
    clamp_field("feedforward_torque", -lim.max_torque, lim.max_torque)

    if c.mode == ControlMode.POSITION:
        if not finite(c.position):
            raise ValueError("position mode requires a finite target position")
        clamp_field("position", pmin, pmax)
        c.velocity = c.velocity if finite(c.velocity) else 0.0
        clamp_field("velocity", -lim.max_velocity, lim.max_velocity)
        c.velocity_limit = _limit(c.velocity_limit, lim.max_velocity)
        c.accel_limit = _limit(c.accel_limit, lim.max_accel)
    elif c.mode == ControlMode.VELOCITY:
        c.position = NAN
        c.velocity = c.velocity if finite(c.velocity) else 0.0
        clamp_field("velocity", -lim.max_velocity, lim.max_velocity)
        c.velocity_limit = _limit(c.velocity_limit, lim.max_velocity)
        c.accel_limit = _limit(c.accel_limit, lim.max_accel)
    elif c.mode == ControlMode.TORQUE:
        c.position = NAN
        c.velocity = 0.0
        c.kp_scale = 0.0
        c.kd_scale = 0.0
    elif c.mode == ControlMode.STAY_WITHIN:
        lo = c.lower_bound if finite(c.lower_bound) else pmin
        hi = c.upper_bound if finite(c.upper_bound) else pmax
        lo, hi = _clamp(lo, pmin, pmax), _clamp(hi, pmin, pmax)
        if math.isfinite(lo) and math.isfinite(hi) and lo > hi:
            raise ValueError("lower bound must be <= upper bound")
        c.lower_bound = lo if math.isfinite(lo) else NAN
        c.upper_bound = hi if math.isfinite(hi) else NAN
    return c, notes


class Severity(enum.IntEnum):
    INFO = 0
    WARNING = 1
    TRIP = 2       # motor forced into its safe state
    ESTOP = 3

    @property
    def label(self) -> str:
        return self.name.title()


@dataclass
class Alarm:
    code: str
    severity: Severity
    motor_id: Optional[int]
    message: str
    raised_at: float = field(default_factory=time.time)
    cleared_at: Optional[float] = None
    acknowledged: bool = False

    @property
    def active(self) -> bool:
        return self.cleared_at is None

    @property
    def key(self) -> tuple[str, Optional[int]]:
        return (self.code, self.motor_id)


class TripAction(str, enum.Enum):
    STOP = "stop"    # torque off (coast) - electrical / thermal / fault / comms
    HOLD = "hold"    # controlled stop + servo position hold at the trip point
                     # (torque limited) - motion violations.  Zero-velocity
                     # damping alone would let an external / gravity load
                     # drive the axis away.


@dataclass
class MotorSafetyState:
    armed: bool = False
    tripped: bool = False
    trip_action: TripAction = TripAction.STOP
    trip_reason: str = ""
    missed_cycles: int = 0
    comm_ok: bool = True
    hold_position: float = NAN       # latched position for TripAction.HOLD


class SafetySupervisor:
    """Pure logic, no I/O.  Driven once per control cycle by the worker."""

    def __init__(self, comm_loss_cycles: int = 10, estop_action: str = "stop"):
        self.comm_loss_cycles = int(comm_loss_cycles)
        self.estop_action = estop_action      # "stop" or "brake"
        self.limits: dict[int, SafetyLimits] = {}
        self.default_limits = SafetyLimits()
        self.motors: dict[int, MotorSafetyState] = {}
        self.estop_latched = False
        self.estop_reason = ""
        self._active: dict[tuple[str, Optional[int]], Alarm] = {}

    # ---- configuration -------------------------------------------------
    def limits_for(self, motor_id: int) -> SafetyLimits:
        return self.limits.get(motor_id, self.default_limits)

    def set_limits(self, motor_id: int, lim: SafetyLimits) -> None:
        errs = validate_limits(lim)
        if errs:
            raise ValueError("; ".join(errs))
        self.limits[motor_id] = lim

    def state(self, motor_id: int) -> MotorSafetyState:
        return self.motors.setdefault(motor_id, MotorSafetyState())

    # ---- operator actions ---------------------------------------------
    def trigger_estop(self, reason: str) -> list[Alarm]:
        self.estop_latched = True
        self.estop_reason = reason
        for st in self.motors.values():
            st.armed = False
        return self._raise("ESTOP", Severity.ESTOP, None, f"Emergency stop: {reason}")

    def reset_estop(self) -> list[Alarm]:
        self.estop_latched = False
        self.estop_reason = ""
        return self._clear("ESTOP", None)

    def can_arm(self, motor_id: int, telem: Optional[Telemetry]) -> tuple[bool, str]:
        st = self.state(motor_id)
        if self.estop_latched:
            return False, "Emergency stop is latched"
        if st.tripped:
            return False, f"Motor is tripped: {st.trip_reason}"
        if not st.comm_ok or telem is None:
            return False, "No communication with motor"
        if telem.mode == FirmwareMode.FAULT:
            return False, f"Controller fault {telem.fault_text or fault_title(telem.fault)}"
        return True, ""

    def reset_trip(self, motor_id: int, telem: Optional[Telemetry]) -> tuple[bool, str, list[Alarm]]:
        """Acknowledge a trip.  Returns (ok, reason, cleared alarms)."""
        st = self.state(motor_id)
        if self.estop_latched:
            return False, "Reset the emergency stop first", []
        holding = st.tripped and st.trip_action == TripAction.HOLD and finite(st.hold_position)
        allowed = {FirmwareMode.STOPPED, FirmwareMode.FAULT, FirmwareMode.TIMEOUT,
                   FirmwareMode.ZERO_VELOCITY, FirmwareMode.BRAKE}
        if holding:
            allowed.add(FirmwareMode.POSITION)
        if telem is not None and telem.mode not in allowed:
            return False, "Motor must be stopped before reset", []
        st.tripped = False
        st.trip_reason = ""
        st.armed = False
        cleared = []
        for key in [k for k in self._active if k[1] == motor_id
                    and self._active[k].severity >= Severity.TRIP]:
            a = self._active.pop(key)
            a.cleared_at = time.time()
            cleared.append(a)
        return True, "", cleared

    def clear_for_new_connection(self, motor_id: int) -> list[Alarm]:
        """A new connection starts clean: motors are disabled anyway, and a
        fault that still exists re-trips from the first telemetry."""
        st = self.state(motor_id)
        st.tripped, st.trip_reason, st.armed = False, "", False
        st.comm_ok, st.missed_cycles, st.hold_position = True, 0, NAN
        cleared = []
        for key in [k for k in self._active if k[1] == motor_id]:
            a = self._active.pop(key)
            a.cleared_at = time.time()
            cleared.append(a)
        return cleared

    # ---- per-cycle evaluation -----------------------------------------
    def safe_command(self, motor_id: int) -> MotorCommand:
        st = self.state(motor_id)
        if self.estop_latched:
            return MotorCommand(mode=ControlMode.BRAKE if self.estop_action == "brake"
                                else ControlMode.STOP)
        if st.trip_action == TripAction.HOLD:
            return self.hold_command(motor_id)
        return MotorCommand(mode=ControlMode.STOP)

    def hold_command(self, motor_id: int) -> MotorCommand:
        """Controlled stop and position hold at the latched trip position."""
        st = self.state(motor_id)
        lim = self.limits_for(motor_id)
        if not finite(st.hold_position):
            return MotorCommand(mode=ControlMode.HOLD)
        return MotorCommand(mode=ControlMode.POSITION, position=st.hold_position, velocity=0.0,
                            velocity_limit=lim.max_velocity, accel_limit=lim.max_accel,
                            maximum_torque=lim.max_torque)

    def evaluate(self, motor_id: int, telem: Optional[Telemetry],
                 filtered: Optional[dict[str, float]],
                 active_mode: ControlMode) -> list[Alarm]:
        """Check one motor's latest feedback.  Returns alarm transitions
        (raised or cleared) produced by this cycle."""
        st = self.state(motor_id)
        lim = self.limits_for(motor_id)
        events: list[Alarm] = []

        if telem is None:
            st.missed_cycles += 1
            if st.missed_cycles >= self.comm_loss_cycles and st.comm_ok:
                st.comm_ok = False
                events += self._trip(motor_id, "COMM_LOSS", TripAction.STOP,
                                     f"No reply for {st.missed_cycles} cycles")
            return events

        if not st.comm_ok:
            # Journal entry only (already cleared): the COMM_LOSS trip itself
            # stays latched until the operator resets it.
            note = Alarm(code="COMM_RESTORED", severity=Severity.INFO, motor_id=motor_id,
                         message="Communication restored – reset the trip to continue")
            note.cleared_at = note.raised_at
            events.append(note)
        st.missed_cycles = 0
        st.comm_ok = True

        f = filtered or {}

        def val(name: str) -> float:
            v = f.get(name, math.nan)
            return v if math.isfinite(v) else getattr(telem, name)

        moving = active_mode in (ControlMode.POSITION, ControlMode.VELOCITY,
                                 ControlMode.TORQUE, ControlMode.STAY_WITHIN)

        # Controller-reported fault / timeout.
        if telem.mode == FirmwareMode.FAULT:
            events += self._trip(motor_id, "FW_FAULT", TripAction.STOP,
                                 f"Controller fault {telem.fault_text or fault_title(telem.fault)}")
        else:
            events += self._clear("FW_FAULT", motor_id, only_if_not_tripping=True)
        if telem.mode == FirmwareMode.TIMEOUT and st.armed:
            events += self._trip(motor_id, "FW_TIMEOUT", TripAction.STOP,
                                 "Controller watchdog timeout (commands not received in time)")
        if telem.fault and not telem.fault_text and is_limit_code(telem.fault) and moving:
            events += self._raise("LIMITING", Severity.INFO, motor_id, fault_title(telem.fault))
        else:
            events += self._clear("LIMITING", motor_id)

        # Supply voltage.
        v = val("voltage")
        if finite(v):
            if v > lim.max_voltage:
                events += self._trip(motor_id, "OVERVOLTAGE", TripAction.STOP,
                                     f"Bus voltage {v:.2f} V > {lim.max_voltage:.2f} V")
            elif v < lim.min_voltage:
                events += self._trip(motor_id, "UNDERVOLTAGE", TripAction.STOP,
                                     f"Bus voltage {v:.2f} V < {lim.min_voltage:.2f} V")
            else:
                band = 0.05 * (lim.max_voltage - lim.min_voltage)
                if v > lim.max_voltage - band or v < lim.min_voltage + band:
                    events += self._raise("VOLTAGE_MARGIN", Severity.WARNING, motor_id,
                                          f"Bus voltage {v:.1f} V near limit")
                else:
                    events += self._clear("VOLTAGE_MARGIN", motor_id)

        # Temperatures.
        for name, limit, code in (("temperature", lim.max_board_temp, "BOARD_TEMP"),
                                  ("motor_temperature", lim.max_motor_temp, "MOTOR_TEMP")):
            t = val(name)
            if not finite(t):
                continue
            if t > limit:
                events += self._trip(motor_id, code, TripAction.STOP,
                                     f"{name.replace('_', ' ')} {t:.1f} °C > {limit:.1f} °C")
            elif t > limit - 10.0:
                events += self._raise(code + "_WARN", Severity.WARNING, motor_id,
                                      f"{name.replace('_', ' ')} {t:.1f} °C approaching limit")
            else:
                events += self._clear(code + "_WARN", motor_id)

        # Over-speed (on filtered velocity, so a single noisy frame does
        # not trip the machine).
        vel = val("velocity")
        if finite(vel) and abs(vel) > lim.overspeed_factor * lim.max_velocity:
            events += self._trip(motor_id, "OVERSPEED", TripAction.HOLD,
                                 f"|velocity| {abs(vel):.3f} rev/s > "
                                 f"{lim.overspeed_factor:.2f} × {lim.max_velocity:.3f}")

        # Soft position limits: trip only when outside *and* moving outward.
        pos = telem.position
        if finite(pos) and moving:
            out_low = finite(lim.position_min) and pos < lim.position_min and (vel if finite(vel) else 0) <= 0
            out_high = finite(lim.position_max) and pos > lim.position_max and (vel if finite(vel) else 0) >= 0
            margin = 0.02 * (abs(lim.position_max - lim.position_min)
                             if finite(lim.position_min) and finite(lim.position_max) else 1.0)
            if (out_low and pos < lim.position_min - margin) or (out_high and pos > lim.position_max + margin):
                events += self._trip(motor_id, "SOFT_LIMIT", TripAction.HOLD,
                                     f"Position {pos:+.4f} rev outside soft limits "
                                     f"[{lim.position_min:+.4f}, {lim.position_max:+.4f}]")

        # Following error.
        if finite(lim.max_following_error) and active_mode == ControlMode.POSITION:
            fe = val("position_error")
            if finite(fe) and abs(fe) > lim.max_following_error:
                events += self._trip(motor_id, "FOLLOWING_ERROR", TripAction.HOLD,
                                     f"Following error {fe:+.4f} rev > ±{lim.max_following_error:.4f}")
        if (st.tripped and st.trip_action == TripAction.HOLD and not finite(st.hold_position)
                and finite(telem.position)):
            st.hold_position = telem.position
        return events

    # ---- alarm bookkeeping --------------------------------------------
    def _trip(self, motor_id: int, code: str, action: TripAction, message: str) -> list[Alarm]:
        st = self.state(motor_id)
        if not st.tripped:
            st.tripped = True
            st.trip_action = action
            st.trip_reason = message
            st.armed = False
            st.hold_position = NAN       # latched by evaluate() from feedback
        elif action == TripAction.STOP:
            st.trip_action = TripAction.STOP   # escalate, never de-escalate
        return self._raise(code, Severity.TRIP, motor_id, message)

    def _raise(self, code: str, sev: Severity, motor_id: Optional[int], message: str) -> list[Alarm]:
        key = (code, motor_id)
        if key in self._active:
            return []
        a = Alarm(code=code, severity=sev, motor_id=motor_id, message=message)
        self._active[key] = a
        return [a]

    def _clear(self, code: str, motor_id: Optional[int], only_if_not_tripping: bool = False) -> list[Alarm]:
        a = self._active.get((code, motor_id))
        if a is None:
            return []
        if only_if_not_tripping and a.severity >= Severity.TRIP:
            return []      # trips clear only via reset_trip()
        self._active.pop((code, motor_id))
        a.cleared_at = time.time()
        return [a]

    def active_alarms(self) -> list[Alarm]:
        return sorted(self._active.values(), key=lambda a: (-a.severity, a.raised_at))
