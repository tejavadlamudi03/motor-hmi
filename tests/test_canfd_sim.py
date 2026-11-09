"""HMI <-> Motor Simulator over a virtual CAN-FD bus (python-can udp_multicast).

This exercises the production hardware path: BusWorker -> MoteusBackend ->
moteus library -> python-can -> bus -> simulated firmware."""

import asyncio
import random
import time

import moteus
import pytest

from moteus_hmi.core.moteus_backend import MoteusBackend
from moteus_hmi.core.types import ControlMode, FirmwareMode, MotorCommand, TransportConfig
from moteus_hmi.sim.canfd_sim import CanFdSimulator, VirtualMoteus, pad_fd


@pytest.fixture
def channel():
    # A private bus per test (group + UDP port), so a simulator that is
    # running on the default bus, or a parallel test, never cross-talks.
    return (f"239.74.{random.randint(1, 250)}.{random.randint(1, 250)}", random.randint(20000, 40000))


@pytest.fixture
def sim(channel):
    s = CanFdSimulator(ids=(1, 2), channel=channel[0], port=channel[1])
    s.start()
    yield s
    s.stop()


def test_pad_fd_lengths():
    assert len(pad_fd(b"\x01" * 9)) == 12 and len(pad_fd(b"\x01" * 49)) == 64 and len(pad_fd(b"")) == 0


def test_node_decodes_real_moteus_frames():
    import moteus
    from moteus_hmi.core.moteus_backend import build_moteus_command, make_query_resolution
    c = moteus.Controller(id=1, query_resolution=make_query_resolution())
    node = VirtualMoteus(1)
    cmd = MotorCommand(mode=ControlMode.POSITION, position=0.3, velocity_limit=1.0, accel_limit=5.0,
                       maximum_torque=0.4, watchdog_timeout=0.25)
    reply = node.handle(bytes(build_moteus_command(c, cmd).data), time.monotonic(), True)
    assert node.last.firmware_mode == FirmwareMode.POSITION
    assert node.motor.cmd.position == pytest.approx(0.3 - node.motor.offset)
    assert node.motor.cmd.maximum_torque == pytest.approx(0.4)
    assert len(reply) <= 64
    parsed = moteus.parse_registers(reply).response
    assert parsed[int(moteus.Register.MODE)] == FirmwareMode.POSITION
    assert parsed[int(moteus.Register.VOLTAGE)] == pytest.approx(24.0, abs=0.5)


def test_backend_over_virtual_bus(sim, channel):
    async def go():
        b = MoteusBackend(TransportConfig(kind="virtual_canfd", virtual_channel=channel[0],
                                          virtual_port=channel[1]))
        await b.open()
        devs = await b.discover()
        assert [d.motor_id for d in devs] == [1, 2] and all(d.uuid for d in devs)
        b.attach(devs)
        cmd = MotorCommand(mode=ControlMode.POSITION, position=0.4, velocity_limit=1.0, accel_limit=5.0,
                           maximum_torque=0.5, watchdog_timeout=0.25)
        for _ in range(120):
            t = await b.cycle({1: cmd}, 0.1)
            await asyncio.sleep(0.01)
        assert t[1].position == pytest.approx(0.4, abs=0.01) and t[1].mode == FirmwareMode.POSITION
        assert t[2].mode == FirmwareMode.STOPPED
        assert float(await b.diagnostic(1, "conf get servo.pid_position.kp", True)) == 4.0
        await b.diagnostic(1, "conf set servo.pid_position.kd 0.07")
        assert "servo.pid_position.kd 0.07" in await b.diagnostic(1, "conf enumerate", timeout_s=20)
        fw = await b.read_channel(1, "firmware")        # version read-out (binary telemetry)
        assert fw["version"] == 0x010100 and fw["model"] == 0x0A and len(fw["serial_number"]) == 3
        assert (await b.read_channel(2, "git"))["dirty"] is False
        await asyncio.sleep(0.4)                       # stop commanding -> firmware watchdog
        assert (await b.cycle({}, 0.1))[1].mode == FirmwareMode.TIMEOUT
        await b.close()
    asyncio.run(go())


class Worker:
    def __init__(self, app, tmp_path, channel):
        from moteus_hmi.core.bus_worker import BusWorker
        self.app, self.w = app, BusWorker(tmp_path)
        self.res, self.alarms, self.batches = {}, [], []
        self.w.request_done.connect(lambda rid, ok, r: self.res.__setitem__(rid, (ok, r)))
        self.w.alarm.connect(self.alarms.append)
        self.w.telemetry.connect(self.batches.append)
        self.w.start()
        ok, r = self.wait(self.w.connect_bus("moteus_r4_11", TransportConfig(
            profile="moteus_r4_11", kind="virtual_canfd", virtual_channel=channel[0],
            virtual_port=channel[1]), 100, 0.25), 10)
        assert ok, r

    def spin(self, t):
        end = time.time() + t
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)

    def wait(self, rid, t=5):
        end = time.time() + t
        while rid not in self.res and time.time() < end:
            self.spin(0.01)
        return self.res[rid]


@pytest.fixture
def hw(qapp, tmp_path, sim, channel):
    w = Worker(qapp, tmp_path, channel)
    yield w
    w.w.shutdown()


def test_worker_motion_estop_and_faults(hw, sim):
    w = hw.w
    assert hw.wait(w.arm(1, True))[0]
    hw.wait(w.set_command(1, MotorCommand(mode=ControlMode.VELOCITY, velocity=0.5)))
    hw.spin(1.0)
    assert sim.nodes[1].motor.vel == pytest.approx(0.5, abs=0.1)        # plant really moves
    assert sim.nodes[1].last.firmware_mode == FirmwareMode.POSITION      # HMI -> CAN -> sim
    w.estop("test")
    hw.spin(0.5)
    assert sim.nodes[1].motor.mode == FirmwareMode.STOPPED
    hw.wait(w.reset_estop())
    # injected driver fault -> trip, fault stays latched (not auto-cleared)
    assert hw.wait(w.arm(2, True))[0]
    sim.nodes[2].motor.injected_fault = 33
    hw.spin(0.5)
    st = hw.batches[-1].status[2]
    assert st.tripped and any(a.code == "FW_FAULT" for a in hw.alarms)
    assert sim.nodes[2].motor.fault == 33


def test_diagnostics_do_not_starve_control_loop(hw, sim):
    """Reading parameters / version must never cause a false comm-loss trip."""
    w = hw.w
    assert hw.wait(w.arm(1, True))[0]
    for ch in ("firmware", "git", "servo_stats"):
        assert hw.wait(w.read_channel(1, ch), 10)[0]
    assert hw.wait(w.diagnostic(1, "conf enumerate", timeout_s=20), 20)[0]
    hw.spin(0.3)
    assert not any(a.code == "COMM_LOSS" for a in hw.alarms), [a.message for a in hw.alarms]
    assert hw.batches[-1].status[1].armed


def test_cable_cut_trips_comm_loss(hw, sim):
    assert hw.wait(hw.w.arm(1, True))[0]
    assert hw.wait(hw.w.arm(2, True))[0]
    sim.nodes[1].motor.unresponsive = True
    hw.spin(0.8)
    assert any(a.code == "COMM_LOSS" and a.motor_id == 1 for a in hw.alarms)
    # fault isolation: the healthy axis keeps running
    assert not any(a.code == "COMM_LOSS" and a.motor_id == 2 for a in hw.alarms)
    assert hw.batches[-1].status[2].armed and hw.batches[-1].status[2].comm_ok
    # The controller's own watchdog stops the motor when commands stop arriving.
    sim.nodes[1].motor.unresponsive = False


def test_undervoltage_trips(hw, sim):
    assert hw.wait(hw.w.arm(1, True))[0]
    sim.nodes[1].motor.supply_voltage = 8.5
    hw.spin(0.6)
    assert any(a.code == "UNDERVOLTAGE" for a in hw.alarms)


def test_change_id_moves_node(hw, sim):
    w = hw.w
    ok, ids = hw.wait(w.change_id(2, 5), 15)
    assert ok, ids
    assert ids == [1, 5] and 5 in sim.nodes and sim.nodes[5].motor.config["id.id"] == "5"
    assert not hw.wait(w.change_id(1, 5))[0]          # duplicate refused


# ---- operator workflows over the virtual bus (formerly in-process tests) ----
def test_cannot_move_without_enable(hw):
    ok, msg = hw.wait(hw.w.set_command(1, MotorCommand(mode=ControlMode.VELOCITY, velocity=1)))
    assert not ok and "not enabled" in msg


def test_position_move_and_clamp(hw, sim):
    from moteus_hmi.core.safety import SafetyLimits
    w = hw.w
    hw.wait(w.set_limits(1, SafetyLimits(max_velocity=1.0, max_accel=5.0, max_torque=1.0)))
    assert hw.wait(w.arm(1, True))[0]
    assert hw.wait(w.set_command(1, MotorCommand(mode=ControlMode.POSITION, position=0.5, velocity_limit=20)))[0]
    hw.spin(2.0)
    raw = hw.batches[-1].status[1].sample.raw
    assert raw.position == pytest.approx(0.5, abs=0.01)
    # the velocity limit the HMI sent was clamped to the safety limit (1.0, not 20)
    assert sim.nodes[1].last.registers[int(moteus.Register.COMMAND_VELOCITY_LIMIT)] == pytest.approx(1.0)
    ok, notes = hw.wait(w.set_command(1, MotorCommand(mode=ControlMode.VELOCITY, velocity=10)))
    assert ok and "clamped" in notes[0]
    hw.spin(1.0)
    assert abs(sim.nodes[1].motor.vel) < 1.2


def test_overtemperature_fault_trips_and_stays_latched(hw, sim):
    assert hw.wait(hw.w.arm(1, True))[0]
    with sim.lock:                     # the 500 Hz physics thread also writes temp
        sim.nodes[1].motor.temp = 90.0
    hw.spin(0.6)
    st = hw.batches[-1].status[1]
    assert st.tripped and not st.armed
    assert sim.nodes[1].motor.mode == FirmwareMode.FAULT       # not auto-cleared by the HMI
    ok, why = hw.wait(hw.w.set_command(1, MotorCommand(mode=ControlMode.VELOCITY, velocity=0.1)))
    assert not ok and "tripped" in why                          # tells the operator WHY
    with sim.lock:
        sim.nodes[1].motor.temp = 30.0
    assert hw.wait(hw.w.reset_motor(1))[0]                     # operator reset clears it
    hw.spin(0.4)
    assert sim.nodes[1].motor.mode == FirmwareMode.STOPPED


def test_diagnostic_injection_rejected(hw):
    ok, msg = hw.wait(hw.w.diagnostic(1, "conf get id.id\nd pos 1 0 1"))
    assert not ok and "control characters" in msg


def test_step_test_metrics(hw):
    assert hw.wait(hw.w.arm(1, True))[0]
    hw.spin(0.3)
    ok, res = hw.wait(hw.w.run_step_test(1, 0.2, 1.5, 2.0, 20.0), t=10)
    assert ok, res
    m = res["metrics"]
    assert m.rise_time_s > 0 and abs(m.steady_state_error) < 0.01


def test_motion_trip_holds_position_under_load(hw, sim):
    """Regression: a following-error trip under external load must hold the
    axis (servo position hold), not let the load drive it away."""
    import math
    from moteus_hmi.core.safety import SafetyLimits
    w = hw.w
    assert hw.wait(w.arm(1, True))[0]
    hw.spin(0.3)
    sim.nodes[1].motor.load_torque = 0.3
    hw.wait(w.set_limits(1, SafetyLimits(max_velocity=2.0, max_accel=10.0, max_torque=0.5,
                                         max_following_error=0.02)))
    hw.spin(1.0)
    st = hw.batches[-1].status[1]
    assert st.tripped and "Following error" in st.trip_reason
    p_trip = sim.nodes[1].motor.pos
    hw.spin(1.5)
    assert abs(sim.nodes[1].motor.pos - p_trip) < 0.1          # held, no runaway
    assert sim.nodes[1].motor.mode == FirmwareMode.POSITION
    # acknowledge: the axis stays enabled and held, alarms are cleared
    hw.wait(w.set_limits(1, SafetyLimits(max_velocity=2.0, max_accel=10.0, max_torque=0.5)))
    ok, how = hw.wait(w.reset_motor(1))
    assert ok and how == "holding"
    hw.spin(0.5)
    assert hw.batches[-1].status[1].armed and not hw.batches[-1].status[1].tripped
    assert any(a.code == "FOLLOWING_ERROR" and not a.active for a in hw.alarms)
    assert abs(sim.nodes[1].motor.pos - p_trip) < 0.1


def test_wrong_prefix_is_reported_not_silently_connected(qapp, tmp_path, sim, channel):
    """User error seen in the field: CAN prefix set to 1 (thought to be the motor ID)."""
    from moteus_hmi.core.bus_worker import BusWorker
    w = BusWorker(tmp_path)
    res = {}
    w.request_done.connect(lambda rid, ok, r: res.__setitem__(rid, (ok, r)))
    w.start()
    rid = w.connect_bus("moteus_r4_11", TransportConfig(
        profile="moteus_r4_11", kind="virtual_canfd", virtual_channel=channel[0],
        virtual_port=channel[1], can_prefix=1, explicit_ids=[1]), 100, 0.25)
    end = time.time() + 8
    while rid not in res and time.time() < end:
        qapp.processEvents(); time.sleep(0.01)
    w.shutdown()
    ok, msg = res[rid]
    assert not ok and "did not answer" in msg and "CAN prefix" in msg
    assert sim.stats["ignored_prefix"] > 0 and sim.last_ignored_prefix == 1 and sim.stats["rx"] == 0
