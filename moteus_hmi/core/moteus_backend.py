"""Real hardware backend built on the official ``moteus`` python package.

Supported USB-CAN / CAN-FD adapters (CAN-FD is mandatory for moteus):

* mjbots fdcanusb / mjcanfd-usb-1x - serial CDC device, Windows + Linux.
* Any SocketCAN CAN-FD interface on Linux (the new gs_usb fdcanusb
  firmware, PEAK, Kvaser, ...).  Bit timing must be configured with
  ``ip link`` - see docs/HARDWARE.md.
* Any python-can interface with CAN-FD support (``pcan``, ``kvaser``,
  ``vector``, ``ixxat`` ...) - this is the Windows route for non-mjbots
  adapters.  moteus defaults PCAN to 1 Mbit / 5 Mbit timing.

Classic CAN 2.0 adapters and slcan / canable-derived adapters do not work
with moteus (see moteus docs/platforms/socketcan.md).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Optional

from .backend import BackendError, DiagnosticError, MotorBackend
from .types import (ControlMode, DeviceDescriptor, MotorCommand, Telemetry,
                    TransportConfig, finite)

log = logging.getLogger(__name__)


class _IsolationUnavailable(Exception):
    pass


def _opt(v: float) -> Optional[float]:
    """moteus omits a register from the frame when the kwarg is None."""
    return v if finite(v) else None


class _SerializedTransport:
    """Wraps a moteus.Transport so that control cycles and diagnostic
    stream traffic never interleave on the wire.

    Each ``cycle`` is atomic AND bounded: a single exchange may hold the bus
    for at most ``MAX_HOLD_S``.  A diagnostic poll that gets no answer (an
    older firmware, a busy node) therefore delays the control loop by a few
    milliseconds instead of starving it into a false comm-loss trip.  An
    unanswered poll simply returns no frames; the moteus Stream polls again.
    """

    MAX_HOLD_S = 0.05     # a CAN-FD round trip is ~1 ms (fdcanusb: a few ms)

    def __init__(self, transport):
        self._t = transport
        self._lock = asyncio.Lock()

    async def cycle(self, *args, **kwargs):
        async with self._lock:
            try:
                return await asyncio.wait_for(self._t.cycle(*args, **kwargs), self.MAX_HOLD_S)
            except asyncio.TimeoutError:
                return []

    async def read(self, *args, **kwargs):
        async with self._lock:
            return await self._t.read(*args, **kwargs)

    async def write(self, *args, **kwargs):
        async with self._lock:
            return await self._t.write(*args, **kwargs)

    async def flush_read_queue(self, *args, **kwargs):
        async with self._lock:
            return await self._t.flush_read_queue(*args, **kwargs)

    async def discover(self, *args, **kwargs):
        async with self._lock:
            return await self._t.discover(*args, **kwargs)

    def close(self):
        self._t.close()

    def __getattr__(self, name):
        return getattr(self._t, name)


def make_query_resolution():
    """Telemetry set chosen so the reply fits one 64 byte CAN-FD frame
    (59 bytes, verified in tests/test_moteus_backend.py)."""
    import moteus
    qr = moteus.QueryResolution()
    qr.mode = moteus.INT8
    qr.position = moteus.F32
    qr.velocity = moteus.F32
    qr.torque = moteus.F32
    qr.q_current = moteus.F32
    qr.d_current = moteus.F32
    qr.abs_position = moteus.IGNORE
    qr.power = moteus.F32
    qr.motor_temperature = moteus.INT16
    qr.trajectory_complete = moteus.INT8
    qr.home_state = moteus.INT8
    qr.voltage = moteus.INT16
    qr.temperature = moteus.INT16
    qr.fault = moteus.INT8
    qr._extra = {
        moteus.Register.CONTROL_POSITION: moteus.F32,
        moteus.Register.CONTROL_VELOCITY: moteus.F32,
    }
    return qr


def telemetry_from_values(motor_id: int, values: dict, stamp: float) -> Telemetry:
    """Translate a moteus reply register dict into ``Telemetry``."""
    import moteus
    R = moteus.Register

    def f(reg) -> float:
        v = values.get(int(reg))
        try:
            return float(v) if v is not None else math.nan
        except (TypeError, ValueError):
            return math.nan

    def i(reg) -> int:
        v = values.get(int(reg))
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    return Telemetry(
        motor_id=motor_id,
        timestamp=stamp,
        mode=i(R.MODE),
        position=f(R.POSITION),
        velocity=f(R.VELOCITY),
        torque=f(R.TORQUE),
        q_current=f(R.Q_CURRENT),
        d_current=f(R.D_CURRENT),
        power=f(R.POWER),
        motor_temperature=f(R.MOTOR_TEMPERATURE),
        temperature=f(R.TEMPERATURE),
        voltage=f(R.VOLTAGE),
        fault=i(R.FAULT),
        trajectory_complete=bool(i(R.TRAJECTORY_COMPLETE)),
        home_state=i(R.HOME_STATE),
        control_position=f(R.CONTROL_POSITION),
        control_velocity=f(R.CONTROL_VELOCITY),
    )


def build_moteus_command(controller, cmd: MotorCommand):
    """Encode a (supervisor-clamped) MotorCommand with the moteus library."""
    m = cmd.mode
    if m == ControlMode.QUERY:
        return controller.make_query()
    if m == ControlMode.STOP:
        return controller.make_stop(query=True)
    if m == ControlMode.BRAKE:
        return controller.make_brake(query=True)
    if m == ControlMode.HOLD:
        return controller.make_zero_velocity(kd_scale=_opt(cmd.kd_scale), query=True)
    if m == ControlMode.STAY_WITHIN:
        return controller.make_stay_within(
            lower_bound=_opt(cmd.lower_bound),
            upper_bound=_opt(cmd.upper_bound),
            feedforward_torque=_opt(cmd.feedforward_torque),
            kp_scale=_opt(cmd.kp_scale),
            kd_scale=_opt(cmd.kd_scale),
            maximum_torque=_opt(cmd.maximum_torque),
            watchdog_timeout=_opt(cmd.watchdog_timeout),
            query=True)
    if m in (ControlMode.POSITION, ControlMode.VELOCITY, ControlMode.TORQUE):
        # position=NaN means "hold the current control position and
        # integrate velocity" in moteus; it must be sent explicitly.
        return controller.make_position(
            position=cmd.position if m == ControlMode.POSITION else math.nan,
            velocity=cmd.velocity if finite(cmd.velocity) else 0.0,
            feedforward_torque=_opt(cmd.feedforward_torque),
            kp_scale=_opt(cmd.kp_scale),
            kd_scale=_opt(cmd.kd_scale),
            maximum_torque=_opt(cmd.maximum_torque),
            watchdog_timeout=_opt(cmd.watchdog_timeout),
            velocity_limit=_opt(cmd.velocity_limit) if m != ControlMode.TORQUE else None,
            accel_limit=_opt(cmd.accel_limit) if m != ControlMode.TORQUE else None,
            query=True)
    raise BackendError(f"unsupported mode {m}")


class MoteusBackend(MotorBackend):
    def __init__(self, config: TransportConfig):
        super().__init__(config)
        self._raw_transport = None
        self._transport: Optional[_SerializedTransport] = None
        self._controllers: dict[int, Any] = {}
        self._streams: dict[int, Any] = {}
        self._stream_locks: dict[int, asyncio.Lock] = {}
        self._qr = None
        self._description = ""
        self._frames = [{}, {}, 0, 0]   # last tx hex/motor, last rx hex/motor, tx bytes, rx bytes

    # ---------------------------------------------------------------
    def _make_devices(self) -> list:
        import moteus
        cfg = self.config
        kind = cfg.kind
        if kind == "fdcanusb":
            path = cfg.fdcanusb_path or None
            if not path:
                found = moteus.FdcanusbDevice.detect_fdcanusbs()
                if not found:
                    raise BackendError("No fdcanusb detected. Check the USB cable / driver.")
                path = found[0]
            return [moteus.FdcanusbDevice(path, disable_brs=cfg.disable_brs)]
        if kind == "socketcan":
            return [moteus.PythonCanDevice(interface="socketcan", channel=cfg.can_channel,
                                           disable_brs=cfg.disable_brs)]
        if kind == "pythoncan":
            return [moteus.PythonCanDevice(interface=cfg.can_interface, channel=cfg.can_channel,
                                           disable_brs=cfg.disable_brs)]
        if kind == "virtual_canfd":
            # Same driver stack as a real adapter, over python-can's
            # udp_multicast virtual CAN-FD bus to the Motor Simulator app.
            return [moteus.PythonCanDevice(interface="udp_multicast", channel=cfg.virtual_channel,
                                           port=int(cfg.virtual_port), disable_brs=cfg.disable_brs)]
        if kind == "auto":
            errors = []
            try:
                paths = moteus.FdcanusbDevice.detect_fdcanusbs()
                if paths:
                    return [moteus.FdcanusbDevice(p, disable_brs=cfg.disable_brs) for p in paths]
            except Exception as e:  # pragma: no cover - hardware specific
                errors.append(f"fdcanusb: {e}")
            try:
                devs = moteus.PythonCanDevice.enumerate_devices(disable_brs=cfg.disable_brs)
                if devs:
                    return list(devs)
            except Exception as e:  # pragma: no cover - hardware specific
                errors.append(f"python-can: {e}")
            raise BackendError("No CAN-FD adapter found. " + "; ".join(errors))
        raise BackendError(f"Unknown transport kind {kind!r}")

    async def open(self) -> str:
        import moteus
        try:
            devices = self._make_devices()
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"Could not open CAN adapter: {e}") from e
        self._raw_transport = moteus.Transport(devices)
        self._transport = _SerializedTransport(self._raw_transport)
        self._qr = make_query_resolution()
        self._description = ", ".join(repr(d) for d in devices)
        return self._description

    async def discover(self) -> list[DeviceDescriptor]:
        import moteus
        assert self._transport is not None
        cfg = self.config
        if cfg.explicit_ids:
            out = []
            for cid in cfg.explicit_ids:
                c = moteus.Controller(id=cid, transport=self._transport,
                                      can_prefix=cfg.can_prefix)
                try:
                    r = await asyncio.wait_for(c.query(), 0.2)
                    ok = r is not None
                except (asyncio.TimeoutError, Exception):
                    ok = False
                out.append(DeviceDescriptor(motor_id=cid, address=cid, responding=ok))
            return out

        infos = await self._transport.discover(can_prefix=cfg.can_prefix)
        out = []
        seen: dict[int, int] = {}
        for info in infos:
            seen[info.can_id] = seen.get(info.can_id, 0) + 1
        for info in infos:
            uuid_hex = info.uuid.hex() if info.uuid else ""
            if seen[info.can_id] > 1:
                # Same CAN id on several buses / devices: the HMI keys
                # everything by CAN id, so refuse ambiguous setups.
                raise BackendError(
                    f"Duplicate CAN ID {info.can_id} detected. Give every controller a "
                    "unique id.id (moteus_tool --target OLD -c, 'conf set id.id N', "
                    "'conf write') before operating.")
            if info.address is None:
                continue
            out.append(DeviceDescriptor(
                motor_id=info.can_id, uuid=uuid_hex,
                bus=repr(info.transport_device) if info.transport_device else "",
                address=info.address))
        return sorted(out, key=lambda d: d.motor_id)

    def attach(self, devices: list[DeviceDescriptor]) -> None:
        import moteus
        self.devices = {d.motor_id: d for d in devices}
        self._controllers = {
            d.motor_id: moteus.Controller(
                id=d.address if d.address is not None else d.motor_id,
                transport=self._transport,
                query_resolution=self._qr,
                can_prefix=self.config.can_prefix)
            for d in devices
        }
        self._streams.clear()

    async def cycle(self, commands, timeout_s):
        assert self._transport is not None
        frames = [build_moteus_command(ctrl, commands.get(mid, MotorCommand()))
                  for mid, ctrl in self._controllers.items()]
        for mid, f in zip(self._controllers, frames):
            self._frames[0][mid] = bytes(f.data).hex(" ").upper()
            self._frames[2] += len(f.data)
        out: dict[int, Optional[Telemetry]] = {mid: None for mid in self._controllers}
        try:
            replies = await self._cycle_isolated(list(zip(self._controllers, frames)), timeout_s)
        except _IsolationUnavailable:
            replies = await self._cycle_batched(frames, timeout_s)
        stamp = time.monotonic()
        for mid, r in replies:
            data = getattr(r, "data", b"") or b""
            self._frames[3] += len(data)
            mid = getattr(r, "id", mid)
            if mid in out and getattr(r, "values", None) is not None:
                out[mid] = telemetry_from_values(mid, r.values, stamp)
                if data:
                    self._frames[1][mid] = bytes(data).hex(" ").upper()
        return out

    async def _cycle_isolated(self, items, timeout_s):
        """One bus transaction for all motors, but replies are collected per
        node: a silent node (cable cut, power loss) only loses ITS reply
        instead of discarding the whole batch and tripping healthy axes."""
        try:
            from moteus.transport_device import TransportDevice
            t = self._raw_transport
            make_frame, make_filter = t._command_to_frame, t._make_response_filter
            devices_for = t._get_devices_for_command
        except (ImportError, AttributeError) as e:      # library internals changed
            raise _IsolationUnavailable() from e
        by_device: dict = {}
        requests = []
        for mid, cmd in items:
            frame = make_frame(cmd)
            flt = make_filter(cmd) if cmd.reply_required else None
            for dev in await devices_for(cmd):
                if dev.parent() is not None:
                    raise _IsolationUnavailable()
                req = TransportDevice.Request(frame=frame, frame_filter=flt,
                                              expected_reply_size=cmd.expected_reply_size)
                by_device.setdefault(dev, []).append(req)
                requests.append((mid, cmd, req))
        async with self._transport._lock:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[d.transaction(reqs) for d, reqs in by_device.items()]), timeout_s)
            except asyncio.TimeoutError:
                pass                     # keep whatever replies did arrive
        out = []
        for mid, cmd, req in requests:
            for frame in req.responses:
                parsed = cmd.parse(frame) if hasattr(cmd, "parse") else frame
                out.append((mid, parsed))
        return out

    async def _cycle_batched(self, frames, timeout_s):
        try:
            results = await asyncio.wait_for(self._transport.cycle(frames), timeout_s)
        except asyncio.TimeoutError:
            return []
        return [(getattr(r, "id", None), r) for r in results or []]

    def last_frames(self):
        tx, rx, txb, rxb = self._frames
        self._frames = [{}, {}, 0, 0]
        return tx, rx, txb, rxb

    async def stop_all(self) -> None:
        if not self._transport or not self._controllers:
            return
        frames = [c.make_stop() for c in self._controllers.values()]
        try:
            await asyncio.wait_for(self._transport.cycle(frames), 0.5)
        except Exception as e:  # never raise from a stop path
            log.warning("stop_all failed: %s", e)

    async def _stream(self, motor_id: int):
        import moteus
        if motor_id not in self._controllers:
            raise BackendError(f"Motor {motor_id} is not attached")
        s = self._streams.get(motor_id)
        if s is None:
            # Plain (non flow-control) polling works on every firmware and
            # avoids the 0.5 s capability probe.
            s = moteus.Stream(self._controllers[motor_id], use_flow_control=False)
            # Same preamble as moteus_tool: silence any telemetry spew.
            await s.write_message(b"tel stop")
            await s.flush_read()
            self._streams[motor_id] = s
        return s

    async def diagnostic(self, motor_id, command, allow_any_response=False, timeout_s=5.0):
        import moteus
        lock = self._stream_locks.setdefault(motor_id, asyncio.Lock())
        async with lock:
            s = await self._stream(motor_id)
            try:
                res = await asyncio.wait_for(
                    s.command(command.encode("latin1"), allow_any_response=allow_any_response),
                    timeout_s)
            except moteus.CommandError as e:
                raise DiagnosticError(e.message) from e
            except asyncio.TimeoutError as e:
                # Drop the stream so a half-read reply cannot poison the next one.
                self._streams.pop(motor_id, None)
                raise BackendError(f"Timeout waiting for reply to {command!r}") from e
            text = res.decode("latin1") if isinstance(res, (bytes, bytearray)) else str(res)
            if allow_any_response and text.startswith("ERR"):
                raise DiagnosticError(text)
            return text

    async def diagnostic_write_only(self, motor_id, command):
        lock = self._stream_locks.setdefault(motor_id, asyncio.Lock())
        async with lock:
            s = await self._stream(motor_id)
            await s.write_message(command.encode("latin1"))
            # The node may not answer on this id any more: drop the stream.
            self._streams.pop(motor_id, None)

    async def read_channel(self, motor_id, name, timeout_s=5.0):
        from moteus.moteus import namedtuple_to_dict
        lock = self._stream_locks.setdefault(motor_id, asyncio.Lock())
        async with lock:
            s = await self._stream(motor_id)
            try:
                data = await asyncio.wait_for(s.read_data(name), timeout_s)
            except asyncio.TimeoutError as e:
                self._streams.pop(motor_id, None)
                raise BackendError(f"Timeout reading channel {name!r}") from e
            return namedtuple_to_dict(data)

    async def set_output(self, motor_id, position, exact):
        c = self._controllers.get(motor_id)
        if c is None:
            raise BackendError(f"Motor {motor_id} is not attached")
        if exact:
            await c.set_output_exact(position=position)
        else:
            await c.set_output_nearest(position=position)

    async def close(self) -> None:
        try:
            await self.stop_all()
        finally:
            if self._raw_transport is not None:
                try:
                    self._raw_transport.close()
                except Exception as e:  # pragma: no cover
                    log.warning("transport close: %s", e)
            self._raw_transport = None
            self._transport = None
            self._controllers.clear()
            self._streams.clear()

    def moteus_tool_args(self) -> list[str]:
        cfg = self.config
        args: list[str] = []
        if cfg.kind == "fdcanusb" and cfg.fdcanusb_path:
            args += ["--fdcanusb", cfg.fdcanusb_path]
        elif cfg.kind == "fdcanusb":
            args += ["--force-transport", "fdcanusb"]
        elif cfg.kind == "socketcan":
            args += ["--can-iface", "socketcan", "--can-chan", cfg.can_channel]
        elif cfg.kind == "pythoncan":
            args += ["--can-iface", cfg.can_interface, "--can-chan", cfg.can_channel]
        elif cfg.kind == "virtual_canfd":
            args += ["--can-iface", "udp_multicast", "--can-chan", cfg.virtual_channel]
        if cfg.disable_brs:
            args += ["--can-disable-brs"]
        if cfg.can_prefix:
            args += ["--can-prefix", str(int(cfg.can_prefix))]
        return args


def list_serial_ports() -> list[tuple[str, str]]:
    """(device, description) for every serial port, fdcanusb first."""
    try:
        import serial.tools.list_ports
        from moteus.fdcanusb_device import FDCANUSB_IDS
    except Exception:  # pragma: no cover
        return []
    ports = []
    for p in serial.tools.list_ports.comports():
        is_fd = (p.vid, p.pid) in FDCANUSB_IDS
        desc = ("fdcanusb  " if is_fd else "") + (p.description or "")
        ports.append((0 if is_fd else 1, p.device, desc.strip()))
    return [(d, desc) for _, d, desc in sorted(ports)]
