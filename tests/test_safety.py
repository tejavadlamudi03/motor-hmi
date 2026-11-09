import math

import pytest

from moteus_hmi.core.safety import (SafetyLimits, SafetySupervisor, Severity, TripAction,
                                    clamp_command, validate_limits)
from moteus_hmi.core.types import ControlMode, FirmwareMode, MotorCommand, Telemetry

LIM = SafetyLimits(max_velocity=2.0, max_accel=10.0, max_torque=0.5, position_min=-1.0, position_max=1.0)


def T(**kw):
    base = dict(motor_id=1, mode=FirmwareMode.POSITION, position=0.0, velocity=0.0, voltage=24.0,
                temperature=30.0)
    base.update(kw)
    return Telemetry(**base)


def test_position_command_is_clamped_and_always_bounded():
    c, notes = clamp_command(MotorCommand(mode=ControlMode.POSITION, position=5.0, velocity=9.0), LIM)
    assert c.position == 1.0 and c.velocity == 2.0
    assert c.velocity_limit == 2.0 and c.accel_limit == 10.0 and c.maximum_torque == 0.5
    assert len(notes) == 2


def test_requested_limits_cannot_exceed_safety_limits():
    c, _ = clamp_command(MotorCommand(mode=ControlMode.POSITION, position=0.2, velocity_limit=50,
                                      accel_limit=500, maximum_torque=9), LIM)
    assert (c.velocity_limit, c.accel_limit, c.maximum_torque) == (2.0, 10.0, 0.5)


def test_torque_mode_disables_gains_and_clamps():
    c, _ = clamp_command(MotorCommand(mode=ControlMode.TORQUE, feedforward_torque=-3), LIM)
    assert c.feedforward_torque == -0.5 and c.kp_scale == 0 and c.kd_scale == 0 and math.isnan(c.position)


def test_position_mode_requires_target():
    with pytest.raises(ValueError):
        clamp_command(MotorCommand(mode=ControlMode.POSITION, position=math.nan), LIM)


def test_limits_validation():
    assert validate_limits(SafetyLimits()) == []
    assert validate_limits(SafetyLimits(max_torque=1e6))
    assert validate_limits(SafetyLimits(min_voltage=40, max_voltage=20))


def test_fault_trips_and_needs_reset():
    sup = SafetySupervisor()
    sup.set_limits(1, LIM)
    ev = sup.evaluate(1, T(mode=FirmwareMode.FAULT, fault=33), None, ControlMode.POSITION)
    assert any(a.code == "FW_FAULT" and a.severity == Severity.TRIP for a in ev)
    assert sup.state(1).tripped
    assert sup.safe_command(1).mode == ControlMode.STOP
    ok, _ = sup.can_arm(1, T())
    assert not ok
    ok, _, cleared = sup.reset_trip(1, T(mode=FirmwareMode.STOPPED))
    assert ok and [a.code for a in cleared] == ["FW_FAULT"] and cleared[0].cleared_at
    assert sup.can_arm(1, T(mode=FirmwareMode.STOPPED))[0]


def test_comm_loss_trips_after_n_cycles():
    sup = SafetySupervisor(comm_loss_cycles=3)
    for _ in range(2):
        assert sup.evaluate(1, None, None, ControlMode.POSITION) == []
    ev = sup.evaluate(1, None, None, ControlMode.POSITION)
    assert ev and ev[0].code == "COMM_LOSS"


def test_overspeed_and_soft_limit_trip_to_hold():
    sup = SafetySupervisor()
    sup.set_limits(1, LIM)
    sup.evaluate(1, T(velocity=3.0), {"velocity": 3.0}, ControlMode.TORQUE)
    st = sup.state(1)
    assert st.tripped and st.trip_action == TripAction.HOLD
    hold = sup.safe_command(1)              # controlled stop + hold at the trip point
    assert hold.mode == ControlMode.POSITION and hold.position == 0.0 and hold.maximum_torque == 0.5
    sup2 = SafetySupervisor()
    sup2.set_limits(1, LIM)
    ev = sup2.evaluate(1, T(position=1.2, velocity=0.1), None, ControlMode.VELOCITY)
    assert any(a.code == "SOFT_LIMIT" for a in ev)


def test_voltage_and_temperature():
    sup = SafetySupervisor()
    sup.set_limits(1, LIM)
    ev = sup.evaluate(1, T(voltage=60.0), None, ControlMode.QUERY)
    assert any(a.code == "OVERVOLTAGE" for a in ev)
    sup2 = SafetySupervisor()
    ev = sup2.evaluate(1, T(temperature=70.0), None, ControlMode.QUERY)
    assert any(a.code == "BOARD_TEMP_WARN" and a.severity == Severity.WARNING for a in ev)


def test_estop_latches_and_overrides():
    sup = SafetySupervisor(estop_action="brake")
    sup.state(1).armed = True
    sup.trigger_estop("test")
    assert not sup.state(1).armed
    assert sup.safe_command(1).mode == ControlMode.BRAKE
    assert not sup.can_arm(1, T())[0]
    sup.reset_estop()
    assert sup.can_arm(1, T())[0]
