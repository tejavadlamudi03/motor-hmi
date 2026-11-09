"""The bus worker: owns the backend, runs the fixed-rate control loop on
its own asyncio thread, enforces safety, filters feedback and publishes
results to the UI through Qt signals (queued across threads).

UI thread  --(thread-safe calls)-->  BusWorker  --(asyncio)-->  backend
UI thread  <--(Qt signals)---------  BusWorker
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

from .backend import BackendError, MotorBackend
from .controllers import ControllerProfile, get_profile, make_backend
from .datalog import DataRecorder
from .safety import Alarm, SafetyLimits, SafetySupervisor, Severity, clamp_command
from .analysis import step_metrics
from .telemetry import Sample, TelemetryProcessor
from .types import (ControlMode, DeviceDescriptor, FirmwareMode, MotorCommand, Telemetry,
                    TransportConfig, finite)

log = logging.getLogger(__name__)


@dataclass
class MotorStatus:
    motor_id: int
    armed: bool
    tripped: bool
    trip_reason: str
    comm_ok: bool
    command: MotorCommand
    sample: Optional[Sample]


@dataclass
class TelemetryBatch:
    """Published to the UI at ``ui_rate_hz``."""
    samples: dict[int, list[Sample]] = field(default_factory=dict)
    status: dict[int, MotorStatus] = field(default_factory=dict)
    loop_hz: float = 0.0
    overruns: int = 0
    estop: bool = False
    bus: dict[str, Any] = field(default_factory=dict)


class BusWorker(QObject):
    connection_changed = Signal(bool, str)       # connected, description
    devices_changed = Signal(list)               # list[DeviceDescriptor]
    telemetry = Signal(object)                   # TelemetryBatch
    alarm = Signal(object)                       # Alarm (raised or cleared)
    message = Signal(str, str)                   # level, text
    request_done = Signal(int, bool, object)     # request id, ok, result / error text
    noise_report = Signal(int, object)           # motor id, list[NoiseReport]
    step_progress = Signal(int, object)          # motor id, partial data

    def __init__(self, recorder_dir, parent=None):
        super().__init__(parent)
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="bus-worker", daemon=True)
        self.backend: Optional[MotorBackend] = None
        self.profile: ControllerProfile = get_profile(None)
        self.config = TransportConfig()
        self.supervisor = SafetySupervisor()
        self.processors: dict[int, TelemetryProcessor] = {}
        self.filter_specs: dict[str, list] = {}
        self.commands: dict[int, MotorCommand] = {}
        self.latest: dict[int, Optional[Sample]] = {}
        self.devices: list[DeviceDescriptor] = []
        self.recorder = DataRecorder(recorder_dir)
        self.rate_hz = 100.0
        self.ui_rate_hz = 30.0
        self.watchdog_s = 0.25
        self._loop_task: Optional[asyncio.Task] = None
        self._pending: dict[int, list[Sample]] = {}
        self._overruns = 0
        self._busy = False          # exclusive engineering operation in progress
        self.audit_hook: Callable[[str, Any, str], None] = lambda action, details, outcome: None
        self._req = 0
        self._req_lock = threading.Lock()
        self._connected = False
        self._bus_stats = {"tx": 0, "rx": 0, "tx_bytes": 0, "rx_bytes": 0, "last_tx": {}, "last_rx": {}}

    # ------------------------------------------------------------------
    # thread plumbing
    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def shutdown(self, timeout: float = 3.0) -> None:
        """Called from the UI thread at exit: stop motors, close the bus."""
        if not self._thread.is_alive():
            return
        fut = asyncio.run_coroutine_threadsafe(self._disconnect(reason="application exit"), self.loop)
        try:
            fut.result(timeout)
        except Exception as e:  # pragma: no cover
            log.error("shutdown: %s", e)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)

    def _call(self, fn: Callable, *args) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def _submit(self, coro_fn: Callable, *args, request_id: Optional[int] = None) -> int:
        """Run ``coro_fn(*args)`` on the worker loop; result arrives via
        ``request_done(request_id, ok, result)``."""
        with self._req_lock:
            self._req += 1
            rid = request_id or self._req

        async def runner():
            try:
                res = await coro_fn(*args)
                self.request_done.emit(rid, True, res)
            except Exception as e:
                log.debug("request %s failed: %s", rid, traceback.format_exc())
                self.request_done.emit(rid, False, str(e) or e.__class__.__name__)

        asyncio.run_coroutine_threadsafe(runner(), self.loop)
        return rid

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # public API (thread-safe, called from the UI thread)
    def connect_bus(self, profile_key: str, cfg: TransportConfig, rate_hz: float, watchdog_s: float) -> int:
        return self._submit(self._connect, profile_key, cfg, rate_hz, watchdog_s)

    def disconnect_bus(self) -> int:
        return self._submit(self._disconnect, "operator")

    def rediscover(self) -> int:
        return self._submit(self._rediscover)

    def estop(self, reason: str) -> None:
        self._call(self._estop, reason)

    def reset_estop(self) -> int:
        return self._submit(self._reset_estop)

    def arm(self, motor_id: int, armed: bool) -> int:
        return self._submit(self._arm, motor_id, armed)

    def reset_motor(self, motor_id: int) -> int:
        return self._submit(self._reset_motor, motor_id)

    def set_command(self, motor_id: int, cmd: MotorCommand) -> int:
        return self._submit(self._set_command, motor_id, cmd)

    def set_limits(self, motor_id: int, lim: SafetyLimits) -> int:
        return self._submit(self._set_limits, motor_id, lim)

    def set_filter(self, motor_id: Optional[int], signal: str, specs: list) -> int:
        return self._submit(self._set_filter, motor_id, signal, specs)

    def diagnostic(self, motor_id: int, command: str, allow_any: bool = False, timeout_s: float = 5.0) -> int:
        return self._submit(self._diagnostic, motor_id, command, allow_any, timeout_s)

    def read_channel(self, motor_id: int, name: str) -> int:
        return self._submit(self._read_channel, motor_id, name)

    def set_output(self, motor_id: int, position: float, exact: bool) -> int:
        return self._submit(self._set_output, motor_id, position, exact)

    def change_id(self, old: int, new: int) -> int:
        return self._submit(self._change_id, old, new)

    def release_bus(self) -> int:
        """Stop everything and close the adapter so an external tool
        (moteus_tool calibration / flashing) can use it."""
        return self._submit(self._release)

    def reacquire_bus(self) -> int:
        return self._submit(self._connect, self.profile.key, self.config, self.rate_hz, self.watchdog_s)

    def run_step_test(self, motor_id: int, amplitude: float, duration_s: float,
                      velocity_limit: float, accel_limit: float) -> int:
        return self._submit(self._step_test, motor_id, amplitude, duration_s, velocity_limit, accel_limit)

    def run_coroutine(self, coro_fn: Callable, *args) -> int:
        return self._submit(coro_fn, *args)

    def start_recording(self, user: str, note: str = "") -> int:
        return self._submit(self._start_recording, user, note)

    def stop_recording(self) -> int:
        return self._submit(self._stop_recording)

    def reset_bus_stats(self) -> None:
        self._call(self._reset_stats)

    # ------------------------------------------------------------------
    # worker-side implementation
    def _reset_stats(self) -> None:
        for k in ("tx", "rx", "tx_bytes", "rx_bytes"):
            self._bus_stats[k] = 0

    async def _connect(self, profile_key: str, cfg: TransportConfig, rate_hz: float, watchdog_s: float):
        if self._connected:
            await self._disconnect("reconnect")
        self.profile = get_profile(profile_key)
        if cfg.kind not in self.profile.links:
            raise BackendError(f"{self.profile.title} cannot use link '{cfg.kind}'")
        self.config = cfg
        self.rate_hz = max(5.0, min(500.0, float(rate_hz)))
        self.watchdog_s = max(0.02, min(1.0, float(watchdog_s)))
        backend = make_backend(self.profile, cfg)
        desc = await backend.open()
        try:
            devices = await backend.discover()
        except Exception:
            await backend.close()
            raise
        silent = [d.motor_id for d in devices if not d.responding]
        devices = [d for d in devices if d.responding]
        hint = (f"Check: CAN prefix (normally 0 – it is NOT the motor ID; now {cfg.can_prefix}), "
                "node IDs, power, wiring and 120 Ω termination"
                + (", and that the Motor Simulator uses the same bus group / port"
                   if cfg.kind == "virtual_canfd" else "") + ".")
        if not devices:
            await backend.close()
            raise BackendError(("Node ID(s) " + ", ".join(map(str, silent)) + " did not answer. "
                                if silent else "Adapter opened, but no controller answered. ") + hint)
        if silent:
            self.message.emit("warning", f"Node ID(s) {silent} did not answer and were skipped. {hint}")
        backend.attach(devices)
        self.backend = backend
        self.devices = devices
        self.commands = {d.motor_id: MotorCommand() for d in devices}
        self.latest = {d.motor_id: None for d in devices}
        self.processors = {d.motor_id: TelemetryProcessor(self.rate_hz, self.filter_specs) for d in devices}
        stale = []
        for d in devices:
            stale += self.supervisor.clear_for_new_connection(d.motor_id)
        for a in stale:
            self.alarm.emit(copy.copy(a))
        if stale:
            self.audit_hook("safety.trips_cleared_on_connect",
                            {"alarms": [f"{a.code}@{a.motor_id}" for a in stale]}, "ok")
        self._connected = True
        self._loop_task = asyncio.ensure_future(self._control_loop())
        self.connection_changed.emit(True, desc)
        self.devices_changed.emit(list(devices))
        self.audit_hook("bus.connect", {"profile": self.profile.key, "link": cfg.describe(),
                                        "devices": [d.motor_id for d in devices]}, "ok")
        return [d.motor_id for d in devices]

    async def _disconnect(self, reason: str = ""):
        task, self._loop_task = self._loop_task, None
        self._connected = False
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self.backend:
            try:
                await self.backend.close()
            finally:
                self.backend = None
                self.audit_hook("bus.disconnect", {"reason": reason}, "ok")
        if self.recorder.active:
            self.recorder.stop()
        for st in self.supervisor.motors.values():
            st.armed = False
        self.connection_changed.emit(False, reason)
        return True

    async def _release(self):
        await self._disconnect("released for external tool")
        return True

    async def _rediscover(self):
        return await self._connect(self.profile.key, self.config, self.rate_hz, self.watchdog_s)

    def _estop(self, reason: str) -> None:
        events = self.supervisor.trigger_estop(reason)
        for mid in self.commands:
            self.commands[mid] = MotorCommand()
        for a in events:
            self.alarm.emit(copy.copy(a))
        self.audit_hook("safety.estop", {"reason": reason}, "ok")
        if self.backend and self._connected:
            asyncio.ensure_future(self._immediate_stop())

    async def _immediate_stop(self):
        if not self.backend:
            return
        cmds = {mid: self.supervisor.safe_command(mid) for mid in self.commands}
        try:
            await self.backend.cycle(cmds, 0.2)
        except Exception as e:
            log.error("immediate stop failed: %s", e)
            await self.backend.stop_all()

    async def _reset_estop(self):
        for a in self.supervisor.reset_estop():
            self.alarm.emit(copy.copy(a))
        return True

    async def _arm(self, motor_id: int, armed: bool):
        st = self.supervisor.state(motor_id)
        s = self.latest.get(motor_id)
        telem = s.raw if s else None
        if armed:
            ok, why = self.supervisor.can_arm(motor_id, telem)
            if not ok:
                raise BackendError(why)
            st.armed = True
            # Servo-on: hold the current position (or zero speed when the
            # controller has no absolute position feedback).
            if self.profile.supports(ControlMode.POSITION) and telem and finite(telem.position):
                self.commands[motor_id] = MotorCommand(mode=ControlMode.POSITION,
                                                       position=telem.position, velocity=0.0)
            elif self.profile.supports(ControlMode.VELOCITY):
                self.commands[motor_id] = MotorCommand(mode=ControlMode.VELOCITY, velocity=0.0)
            else:
                self.commands[motor_id] = MotorCommand(mode=ControlMode.HOLD)
        else:
            st.armed = False
            self.commands[motor_id] = MotorCommand(mode=ControlMode.STOP)
        return armed

    async def _reset_motor(self, motor_id: int):
        sup = self.supervisor
        st = sup.state(motor_id)
        s = self.latest.get(motor_id)
        hold = sup.hold_command(motor_id) if (st.tripped and st.trip_action.value == "hold") else None
        ok, why, cleared = sup.reset_trip(motor_id, s.raw if s else None)
        if not ok:
            raise BackendError(why)
        for a in cleared:
            self.alarm.emit(copy.copy(a))
        if hold is not None and hold.mode == ControlMode.POSITION and s and s.raw.mode == FirmwareMode.POSITION:
            # The axis was being held (possibly against a load): keep it held
            # and enabled rather than dropping torque on acknowledge.
            st.armed = True
            self.commands[motor_id] = hold
            return "holding"
        # A stop command clears latched controller faults (moteus semantics,
        # fault-ack on ST MCSDK).
        self.commands[motor_id] = MotorCommand(mode=ControlMode.STOP)
        return "stopped"

    async def _set_command(self, motor_id: int, cmd: MotorCommand):
        if motor_id not in self.commands:
            raise BackendError(f"Motor {motor_id} not connected")
        if self.supervisor.estop_latched:
            raise BackendError("Emergency stop is latched")
        st = self.supervisor.state(motor_id)
        if cmd.mode not in (ControlMode.STOP, ControlMode.QUERY):
            if st.tripped:
                raise BackendError(f"Motor tripped: {st.trip_reason}")
            if not st.armed:
                raise BackendError("Motor is not enabled")
            if not self.profile.supports(cmd.mode):
                raise BackendError(f"{cmd.mode.label} is not supported by {self.profile.model}")
        clamped, notes = clamp_command(cmd, self.supervisor.limits_for(motor_id))
        self.commands[motor_id] = clamped
        return notes

    async def _set_limits(self, motor_id: int, lim: SafetyLimits):
        # Never exceed the connected controller's electrical rating.
        lim = SafetyLimits.from_dict(lim.to_dict())
        lim.max_voltage = min(lim.max_voltage, self.profile.max_voltage)
        lim.min_voltage = max(lim.min_voltage, self.profile.min_voltage)
        self.supervisor.set_limits(motor_id, lim)
        # Re-clamp the active command against the new limits.
        cmd = self.commands.get(motor_id)
        if cmd is not None and cmd.mode not in (ControlMode.QUERY, ControlMode.STOP):
            self.commands[motor_id], _ = clamp_command(cmd, lim)
        return True

    async def _set_filter(self, motor_id: Optional[int], signal: str, specs: list):
        targets = [motor_id] if motor_id is not None else list(self.processors)
        for mid in targets:
            p = self.processors.get(mid)
            if p:
                p.set_filter(signal, specs)
        self.filter_specs[signal] = list(specs)
        return True

    def _require(self, motor_id: int) -> MotorBackend:
        if not self.backend or not self._connected:
            raise BackendError("Not connected")
        if motor_id not in self.commands:
            raise BackendError(f"Motor {motor_id} not connected")
        return self.backend

    async def _diagnostic(self, motor_id, command, allow_any, timeout_s):
        # The diagnostic channel is line based: a CR/LF inside a command
        # would inject a second (possibly motion) command.
        if any(ch in command for ch in "\r\n\x00") or not command.isprintable():
            raise BackendError("Rejected diagnostic command containing control characters")
        return await self._require(motor_id).diagnostic(motor_id, command, allow_any, timeout_s)

    async def _read_channel(self, motor_id, name):
        return await self._require(motor_id).read_channel(motor_id, name)

    async def _set_output(self, motor_id, position, exact):
        b = self._require(motor_id)
        if self.supervisor.state(motor_id).armed:
            raise BackendError("Disable the motor before re-referencing its position")
        await b.set_output(motor_id, position, exact)
        return True

    async def _change_id(self, old: int, new: int):
        if not (1 <= int(new) <= 126):
            raise BackendError("CAN id must be 1..126")
        if new in self.commands:
            raise BackendError(f"ID {new} is already used on this bus")
        b = self._require(old)
        if self.supervisor.state(old).armed:
            raise BackendError("Disable the motor first")
        # The controller switches id immediately and never answers the old
        # id again, so do not wait for an OK here.
        await b.diagnostic_write_only(old, f"conf set id.id {int(new)}")
        await asyncio.sleep(0.2)
        ids = await self._rediscover()
        if new not in ids:
            raise BackendError(f"Controller did not re-appear on ID {new}; found {ids}")
        await self.backend.diagnostic(new, "conf write")
        return ids

    async def _start_recording(self, user, note):
        return str(self.recorder.start(user, note))

    async def _stop_recording(self):
        p = self.recorder.stop()
        return str(p) if p else ""

    async def _step_test(self, motor_id, amplitude, duration_s, velocity_limit, accel_limit):
        if not self.profile.supports(ControlMode.POSITION):
            raise BackendError("Step test needs position control")
        st = self.supervisor.state(motor_id)
        if not st.armed:
            raise BackendError("Enable the motor first")
        s = self.latest.get(motor_id)
        if not s or not finite(s.raw.position):
            raise BackendError("No position feedback")
        y0 = s.raw.position
        target = y0 + amplitude
        base = MotorCommand(mode=ControlMode.POSITION, position=target, velocity=0.0,
                            velocity_limit=velocity_limit, accel_limit=accel_limit)
        await self._set_command(motor_id, base)
        t0 = time.monotonic()
        t, y, tq, ctrl = [], [], [], []
        while time.monotonic() - t0 < duration_s:
            await asyncio.sleep(1.0 / self.rate_hz)
            if not st.armed or st.tripped or self.supervisor.estop_latched:
                raise BackendError("Step test aborted (motor disabled / tripped)")
            cur = self.latest.get(motor_id)
            if cur:
                t.append(cur.raw.timestamp - t0)
                y.append(cur.raw.position)
                tq.append(cur.raw.torque)
                ctrl.append(cur.raw.control_position)
        await self._set_command(motor_id, base.copy(position=y0))
        m = step_metrics(t, y, y0, target, tq)
        return {"t": t, "y": y, "torque": tq, "control": ctrl, "y0": y0, "target": target, "metrics": m}

    # ------------------------------------------------------------------
    # the control loop
    def _account(self, cmds: dict[int, MotorCommand]) -> None:
        b = self.backend
        n = len(cmds)
        self._bus_stats["tx"] += n
        info = getattr(b, "last_frames", None)
        if callable(info):
            tx, rx, txb, rxb = info()
            self._bus_stats["last_tx"].update(tx)
            self._bus_stats["last_rx"].update(rx)
            self._bus_stats["tx_bytes"] += txb
            self._bus_stats["rx_bytes"] += rxb
        else:
            self._bus_stats["tx_bytes"] += 16 * n

    async def _control_loop(self):
        period = 1.0 / self.rate_hz
        next_t = time.monotonic()
        last_emit = 0.0
        last_noise = 0.0
        hz_count, hz_t0, loop_hz = 0, time.monotonic(), 0.0
        while self._connected and self.backend is not None:
            sup = self.supervisor
            cmds: dict[int, MotorCommand] = {}
            for mid in self.commands:
                st = sup.state(mid)
                if sup.estop_latched:
                    cmd = sup.safe_command(mid)
                elif st.tripped:
                    cmd = sup.safe_command(mid)
                    s = self.latest.get(mid)
                    if (cmd.mode == ControlMode.STOP and s is not None
                            and s.raw.mode in (FirmwareMode.FAULT, FirmwareMode.STOPPED)):
                        # Already de-energised: do not send stop, which would
                        # clear the controller fault before it is acknowledged.
                        cmd = MotorCommand()
                elif not st.armed:
                    c = self.commands[mid]
                    cmd = c if c.mode == ControlMode.STOP else MotorCommand()
                    if c.mode == ControlMode.STOP:
                        # send the stop once, then go back to monitoring
                        self.commands[mid] = MotorCommand()
                else:
                    cmd = self.commands[mid]
                cmd = cmd.copy(watchdog_timeout=self.watchdog_s)
                cmds[mid] = cmd
            try:
                result = await self.backend.cycle(cmds, timeout_s=max(0.02, 0.8 * period + 0.02))
            except Exception as e:
                log.exception("cycle failed")
                self.message.emit("error", f"Bus error: {e}")
                result = {mid: None for mid in cmds}
            self._account(cmds)
            for mid, telem in result.items():
                sample = None
                if telem is not None:
                    self._bus_stats["rx"] += 1
                    sample = self.processors[mid].process(telem)
                    self.latest[mid] = sample
                    self._pending.setdefault(mid, []).append(sample)
                    if self.recorder.active:
                        try:
                            self.recorder.write(sample)
                        except OSError as e:
                            self.recorder.stop()
                            self.message.emit("error", f"Recording stopped: {e}")
                events = sup.evaluate(mid, telem, sample.filtered if sample else None, cmds[mid].mode)
                for a in events:
                    self.alarm.emit(copy.copy(a))
                    if a.severity >= Severity.TRIP and a.active:
                        self.audit_hook("safety.trip", {"motor": mid, "code": a.code,
                                                        "message": a.message}, "tripped")
                        self.commands[mid] = MotorCommand()
            now = time.monotonic()
            hz_count += 1
            if now - hz_t0 >= 1.0:
                loop_hz = hz_count / (now - hz_t0)
                hz_count, hz_t0 = 0, now
            if now - last_emit >= 1.0 / self.ui_rate_hz:
                last_emit = now
                self._emit_batch(loop_hz)
            if now - last_noise >= 1.0:
                last_noise = now
                for mid, p in self.processors.items():
                    self.noise_report.emit(mid, p.noise())
            next_t += period
            delay = next_t - time.monotonic()
            if delay < 0:
                self._overruns += 1
                next_t = time.monotonic()
                delay = 0
            await asyncio.sleep(delay)

    def _emit_batch(self, loop_hz: float) -> None:
        sup = self.supervisor
        batch = TelemetryBatch(samples=self._pending, loop_hz=loop_hz,
                               overruns=self._overruns, estop=sup.estop_latched,
                               bus={**self._bus_stats, "last_tx": dict(self._bus_stats["last_tx"]),
                                    "last_rx": dict(self._bus_stats["last_rx"])})
        self._pending = {}
        for mid in self.commands:
            st = sup.state(mid)
            batch.status[mid] = MotorStatus(
                motor_id=mid, armed=st.armed, tripped=st.tripped, trip_reason=st.trip_reason,
                comm_ok=st.comm_ok, command=self.commands[mid], sample=self.latest.get(mid))
        self.telemetry.emit(batch)
