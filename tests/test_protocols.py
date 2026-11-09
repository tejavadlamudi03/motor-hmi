"""Wire-level checks against the real moteus library and the MCSDK framing."""

import asyncio
import math

import moteus
import pytest

from moteus_hmi.core import st_mcsdk_backend as st
from moteus_hmi.core.controllers import PROFILES, get_profile
from moteus_hmi.core.moteus_backend import (MoteusBackend, build_moteus_command, make_query_resolution,
                                            telemetry_from_values)
from moteus_hmi.core.types import ControlMode, FirmwareMode, MotorCommand, TransportConfig

R = moteus.Register


def ctrl():
    return moteus.Controller(id=1, query_resolution=make_query_resolution())


def test_telemetry_reply_fits_one_canfd_frame():
    assert ctrl()._default_query_reply_size <= 64


@pytest.mark.parametrize("mode", list(ControlMode))
def test_every_command_fits_one_frame(mode):
    cmd = MotorCommand(mode=mode, position=1.0, velocity=0.5, feedforward_torque=0.1, kp_scale=1.0,
                       kd_scale=1.0, maximum_torque=0.5, velocity_limit=2.0, accel_limit=10.0,
                       lower_bound=-1, upper_bound=1, watchdog_timeout=0.25)
    assert len(build_moteus_command(ctrl(), cmd).data) <= 64


def test_position_command_carries_watchdog_and_limits():
    cmd = MotorCommand(mode=ControlMode.POSITION, position=1.25, velocity=0.0, maximum_torque=0.5,
                       velocity_limit=2.0, accel_limit=10.0, watchdog_timeout=0.25)
    p = moteus.parse_registers(build_moteus_command(ctrl(), cmd).data)
    c = p.command
    assert c[int(R.MODE)] == int(moteus.Mode.POSITION)
    assert c[int(R.COMMAND_POSITION)] == pytest.approx(1.25)
    assert c[int(R.COMMAND_TIMEOUT)] == pytest.approx(0.25)
    assert c[int(R.COMMAND_VELOCITY_LIMIT)] == pytest.approx(2.0)
    assert c[int(R.COMMAND_ACCEL_LIMIT)] == pytest.approx(10.0)
    assert c[int(R.COMMAND_POSITION_MAX_TORQUE)] == pytest.approx(0.5)
    assert p.query, "every command must also request telemetry"


def test_velocity_mode_sends_nan_position():
    p = moteus.parse_registers(build_moteus_command(ctrl(), MotorCommand(mode=ControlMode.VELOCITY, velocity=1.0)).data)
    assert math.isnan(p.command[int(R.COMMAND_POSITION)])


def test_stop_and_brake():
    assert moteus.parse_registers(build_moteus_command(ctrl(), MotorCommand(mode=ControlMode.STOP)).data).command[0] == 0
    assert moteus.parse_registers(build_moteus_command(ctrl(), MotorCommand(mode=ControlMode.BRAKE)).data).command[0] == 15


def test_telemetry_parsing():
    t = telemetry_from_values(3, {int(R.MODE): 10, int(R.POSITION): 0.5, int(R.VOLTAGE): 24.0,
                                  int(R.FAULT): 0, int(R.CONTROL_POSITION): 0.45}, 1.0)
    assert t.mode == FirmwareMode.POSITION and t.position == 0.5 and t.voltage == 24.0
    assert t.position_error == pytest.approx(0.05)
    assert math.isnan(t.torque)


def test_moteus_tool_args():
    assert MoteusBackend(TransportConfig(kind="fdcanusb", fdcanusb_path="/dev/x")).moteus_tool_args() == ["--fdcanusb", "/dev/x"]
    assert MoteusBackend(TransportConfig(kind="socketcan", can_channel="can1", can_prefix=2)).moteus_tool_args() == \
        ["--can-iface", "socketcan", "--can-chan", "can1", "--can-prefix", "2"]


def test_profiles_are_consistent():
    for p in PROFILES.values():
        assert p.modes and p.min_voltage < p.max_voltage and "simulation" not in p.links
        if p.driver == "moteus":
            assert "virtual_canfd" in p.links
    assert get_profile("moteus_r4_11").max_voltage == 44.0


# ---- ST MCSDK FCP --------------------------------------------------------
def test_fcp_crc_matches_documented_example():
    # GET_REG 0x1C: "0x02 0x01 0x1C CRC" -> CRC = 0x1F
    assert st.build_frame(st.FC_GET_REG, bytes([0x1C])) == bytes([0x02, 0x01, 0x1C, 0x1F])
    # Fold of the high byte: sum 0x1F0 -> 0xF0 + 0x01
    assert st.fcp_crc(bytes([0xFF, 0xF1])) == 0xF1


def test_fcp_link_round_trip_and_nack():
    port = st.FakeMcsdkPort()
    link = st.FcpLink(port)
    assert link.get_reg("bus_voltage") == 24
    link.set_reg("speed_kp", 1234)
    assert port.regs["speed_kp"] == 1234
    with pytest.raises(st.FcpNack):
        link.transact(st.FC_SET_REG, bytes([0x19, 1, 0]))  # read-only register
    link.set_ramp(600, 2000)
    assert port.ramps == [(600, 2000)]


def test_st_backend_speed_torque_and_faults():
    async def go():
        prof = get_profile("st_ihm08m1")
        b = st.StMcsdkBackend(TransportConfig(profile=prof.key, kind="st_serial", serial_port="x"), prof)
        port = st.FakeMcsdkPort()
        b.link = st.FcpLink(port)
        b.attach(await b.discover())
        await b.cycle({1: MotorCommand(mode=ControlMode.VELOCITY, velocity=10, accel_limit=5)}, 0.2)
        t = (await b.cycle({1: MotorCommand(mode=ControlMode.VELOCITY, velocity=10, accel_limit=5)}, 0.2))[1]
        assert port.regs["status"] == 6 and t.velocity == pytest.approx(10.0)
        assert port.ramps[0] == (600, 2000)
        await b.cycle({1: MotorCommand(mode=ControlMode.TORQUE, feedforward_torque=0.05)}, 0.2)
        assert port.regs["control_mode"] == 0 and port.regs["torque_ref"] != 0
        port.regs.update(status=10, fault_flags=(0x0042 << 16) | 0x0042)
        t = (await b.cycle({1: MotorCommand()}, 0.2))[1]
        assert t.mode == FirmwareMode.FAULT and "Over current" in t.fault_text and "Over voltage" in t.fault_text
        await b.cycle({1: MotorCommand(mode=ControlMode.STOP)}, 0.2)   # STOP on fault = fault ack
        assert port.regs["fault_flags"] == 0
        with pytest.raises(Exception):
            await b.diagnostic(1, "conf write")
    asyncio.run(go())
