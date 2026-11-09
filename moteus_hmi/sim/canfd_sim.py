"""Virtual moteus controllers that speak the real moteus CAN-FD protocol.

The HMI (or ``moteus_tool``, or your own program using the ``moteus``
library) connects to these exactly as to hardware.  Frames travel over any
python-can bus:

* ``udp_multicast`` (default) - a virtual CAN-FD bus between processes on
  the same PC.  No root, no drivers.
* ``socketcan`` ``vcan0`` - Linux virtual CAN (``ip link add vcan0 type vcan;
  ip link set vcan0 mtu 72 up``).
* a real adapter (PCAN, Kvaser, a second fdcanusb …) - turns this PC into a
  fake controller on a physical bus for hardware-in-the-loop tests.

Implemented, per the moteus register protocol (docs/protocol/registers.md):
  * CAN ID layout  (prefix<<16 | 0x8000 reply | source<<8 | destination),
    broadcast discovery on 0x7f with UUID registers;
  * register writes: mode 0/10/12/13/15 and current mode 9, all command
    registers, set-output nearest / exact; command registers revert to their
    defaults on every mode write, like the firmware;
  * register reads, encoded with the firmware scaling for INT8/16/32/F32;
  * the command watchdog -> mode 11 (timeout), latched until stop;
  * the diagnostic stream (non flow-control): ``conf get/set/enumerate/
    write``, ``tel stop/list``, ``d stop``, ``d brake``, ``d rezero``,
    ``d cfg-set-output``.  Changing ``id.id`` moves the node to its new id.
  * binary telemetry channels ``firmware``, ``git``, ``servo_stats``,
    ``drv8323`` (``tel schema/get``) - so firmware version read-out and
    ``moteus_tool --info`` work.
Not implemented: calibration routines and firmware flashing (use the real
controller for those).
"""

from __future__ import annotations

import io
import logging
import math
import struct
import threading
import time
import uuid as uuidlib
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from moteus import multiplex as mp
from moteus.protocol import Register as R, Writer, scale_register

from .plant import SimMotor
from ..core.types import ControlMode, FirmwareMode, MotorCommand, finite

log = logging.getLogger(__name__)

DEFAULT_INTERFACE = "udp_multicast"
DEFAULT_CHANNEL = "239.74.163.2"          # python-can IPv4 default group
DEFAULT_PORT = 43113                      # python-can default; the port identifies the bus
FD_LENGTHS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64]

_POS = {R.POSITION, R.ABS_POSITION, R.CONTROL_POSITION, R.POSITION_ERROR,
        R.ENCODER_0_POSITION, R.ENCODER_1_POSITION, R.ENCODER_2_POSITION,
        R.COMMAND_POSITION, R.COMMAND_STOP_POSITION, R.SET_OUTPUT_NEAREST, R.SET_OUTPUT_EXACT}
_VEL = {R.VELOCITY, R.CONTROL_VELOCITY, R.VELOCITY_ERROR, R.ENCODER_0_VELOCITY,
        R.ENCODER_1_VELOCITY, R.ENCODER_2_VELOCITY, R.COMMAND_VELOCITY, R.COMMAND_VELOCITY_LIMIT}
_TORQUE = {R.TORQUE, R.CONTROL_TORQUE, R.TORQUE_ERROR, R.POSITION_KP, R.POSITION_KI,
           R.POSITION_KD, R.POSITION_FEEDFORWARD, R.POSITION_COMMAND,
           R.COMMAND_FEEDFORWARD_TORQUE, R.COMMAND_POSITION_MAX_TORQUE}
_CURRENT = {R.Q_CURRENT, R.D_CURRENT, R.COMMAND_Q_CURRENT, R.COMMAND_D_CURRENT}
_VOLT = {R.VOLTAGE, R.VOLTAGE_PHASE_A, R.VOLTAGE_PHASE_B, R.VOLTAGE_PHASE_C}
_TEMP = {R.TEMPERATURE, R.MOTOR_TEMPERATURE}

TELEMETRY_CHANNELS = ("servo_stats", "firmware", "drv8323", "git")

SIM_FIRMWARE_VERSION = 0x010100     # moteus ABI version reported by the simulator
SIM_MODEL = 0x0000000A              # moteus-r4 family model code


def _vu(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(text: str) -> bytes:
    raw = text.encode("utf8")
    return _vu(len(raw)) + raw


# type spec -> (schema bytes, struct format or encoder)
_SCALARS = {"bool": (_vu(2), "<B"), "u8": (_vu(4) + b"\x01", "<B"), "u32": (_vu(4) + b"\x04", "<I"),
            "u64": (_vu(4) + b"\x08", "<Q"), "i8": (_vu(3) + b"\x01", "<b"), "f32": (_vu(7), "<f")}


def _schema(spec) -> bytes:
    if isinstance(spec, str):
        return _SCALARS[spec][0]
    kind = spec[0]
    if kind == "array":                       # ("array", n, element)
        return _vu(19) + _vu(spec[1]) + _schema(spec[2])
    if kind == "object":                      # ("object", [(name, spec), ...])
        out = _vu(16) + _vu(0)
        for name, sub in spec[1]:
            out += _vu(0) + _str(name) + _vu(0) + _schema(sub) + b"\x00"
        return out + _vu(0) + _str("") + _vu(0) + _vu(0) + b"\x00"   # final marker
    raise ValueError(spec)


def _data(spec, value) -> bytes:
    if isinstance(spec, str):
        fmt = _SCALARS[spec][1]
        v = value
        if spec in ("bool", "u8", "u32", "u64", "i8"):
            v = int(v) if v is not None and v == v else 0
        return struct.pack(fmt, v)
    if spec[0] == "array":
        return b"".join(_data(spec[2], v) for v in value)
    return b"".join(_data(sub, value.get(name)) for name, sub in spec[1])


CHANNEL_SPECS = {
    "firmware": ("object", [("version", "u32"), ("serial_number", ("array", 3, "u32")), ("model", "u32")]),
    "git": ("object", [("hash", ("array", 20, "u8")), ("dirty", "bool"), ("timestamp", "u64")]),
    "servo_stats": ("object", [(n, t) for n, t in (
        ("mode", "u8"), ("fault", "u8"), ("position", "f32"), ("velocity", "f32"), ("torque_Nm", "f32"),
        ("q_A", "f32"), ("d_A", "f32"), ("bus_V", "f32"), ("filt_bus_V", "f32"), ("fet_temp_C", "f32"),
        ("filt_fet_temp_C", "f32"), ("motor_temp_C", "f32"), ("power_W", "f32"),
        ("control_position", "f32"), ("control_velocity", "f32"))]),
    "drv8323": ("object", [("fault", "bool"), ("vds_ocp", "bool"), ("gdf", "bool"), ("uvlo", "bool"),
                           ("otsd", "bool"), ("vds_ha", "bool"), ("vds_la", "bool")]),
}


def pad_fd(data: bytes) -> bytes:
    n = next(l for l in FD_LENGTHS if l >= len(data))
    return data + bytes([mp.NOP]) * (n - len(data))


def _write_value(w: Writer, reg: int, value, res: int) -> None:
    v = float(value) if value is not None else math.nan
    if reg in _POS:
        w.write_position(v, res)
    elif reg in _VEL:
        w.write_velocity(v, res)
    elif reg in _TORQUE:
        w.write_torque(v, res)
    elif reg in _CURRENT:
        w.write_current(v, res)
    elif reg in _VOLT:
        w.write_voltage(v, res)
    elif reg in _TEMP:
        w.write_temperature(v, res)
    elif reg == R.POWER:
        w.write_power(v, res)
    else:
        w.write_int(int(v) if math.isfinite(v) else 0, res)


@dataclass
class ReceivedCommand:
    """What the client last asked this node to do (for display)."""
    firmware_mode: int = 0
    registers: dict = field(default_factory=dict)
    time: float = 0.0


class VirtualMoteus:
    """One simulated controller."""

    def __init__(self, motor_id: int):
        self.motor = SimMotor(motor_id)
        self.id = motor_id
        self.uuid = uuidlib.UUID(int=(0x51A0C0FFEE << 88) | motor_id).bytes
        # Like the firmware, the UUID is also exposed as the "uuid" config group.
        for i, byte in enumerate(self.uuid):
            self.motor.config[f"uuid.uuid.{i}"] = str(byte)
        self._rx_text: dict[int, bytes] = {}
        self._tx_text: dict[int, bytes] = {}
        self.last = ReceivedCommand()
        self.frames_rx = 0
        self.frames_tx = 0
        self.console: deque[str] = deque(maxlen=200)

    # ---- registers -----------------------------------------------------
    def _read(self, reg: int, now: float):
        m = self.motor
        t = m.telemetry(now)
        table = {
            R.MODE: t.mode, R.POSITION: t.position, R.VELOCITY: t.velocity, R.TORQUE: t.torque,
            R.Q_CURRENT: t.q_current, R.D_CURRENT: t.d_current, R.ABS_POSITION: math.nan,
            R.POWER: t.power, R.MOTOR_TEMPERATURE: t.motor_temperature,
            R.TRAJECTORY_COMPLETE: int(t.trajectory_complete), R.HOME_STATE: t.home_state,
            R.VOLTAGE: t.voltage, R.TEMPERATURE: t.temperature, R.FAULT: t.fault,
            R.CONTROL_POSITION: t.control_position, R.CONTROL_VELOCITY: t.control_velocity,
            R.CONTROL_TORQUE: m.torque, R.POSITION_ERROR: t.position_error,
            R.VELOCITY_ERROR: t.velocity_error, R.TORQUE_ERROR: 0.0,
            R.ENCODER_0_POSITION: t.position, R.ENCODER_0_VELOCITY: t.velocity,
            R.MODEL_NUMBER: 0x00000a, R.FIRMWARE_VERSION: 0x010100, R.REGISTER_MAP_VERSION: 4,
            R.MULTIPLEX_ID: self.id, R.UUID_MASK_CAPABLE: 1,
            R.MILLISECOND_COUNTER: int(now * 1000) & 0x7FFFFFFF,
        }
        if R.UUID1 <= reg <= R.UUID4:
            k = reg - R.UUID1
            return int.from_bytes(self.uuid[4 * k:4 * k + 4], "little", signed=True)
        return table.get(reg, 0)

    def _apply_writes(self, writes: dict, now: float) -> None:
        m = self.motor
        for reg, exact in ((R.SET_OUTPUT_NEAREST, False), (R.SET_OUTPUT_EXACT, True)):
            if reg in writes:
                cur = m.pos + m.offset
                target = float(writes[reg])
                new = target if exact else target + round(cur - target)
                m.offset = new - m.pos
                m.home_state = 2
        if R.MODE not in writes:
            return
        mode = int(writes[R.MODE])
        g = lambda reg, default: float(writes[reg]) if reg in writes and writes[reg] is not None else default
        wd = g(R.COMMAND_TIMEOUT, 0.0)
        # moteus: 0 -> configured default, NaN -> no timeout.
        wd = math.nan if wd == 0.0 else (1e9 if not math.isfinite(wd) else wd)
        cmd = MotorCommand(
            position=g(R.COMMAND_POSITION, math.nan), velocity=g(R.COMMAND_VELOCITY, 0.0),
            feedforward_torque=g(R.COMMAND_FEEDFORWARD_TORQUE, 0.0),
            kp_scale=g(R.COMMAND_KP_SCALE, 1.0), kd_scale=g(R.COMMAND_KD_SCALE, 1.0),
            maximum_torque=g(R.COMMAND_POSITION_MAX_TORQUE, math.nan),
            velocity_limit=g(R.COMMAND_VELOCITY_LIMIT, math.nan),
            accel_limit=g(R.COMMAND_ACCEL_LIMIT, math.nan), watchdog_timeout=wd)
        if mode == FirmwareMode.STOPPED:
            cmd.mode = ControlMode.STOP
        elif mode == FirmwareMode.POSITION:
            if not finite(cmd.position) and cmd.kp_scale == 0 and cmd.kd_scale == 0:
                cmd.mode = ControlMode.TORQUE
            else:
                cmd.mode = ControlMode.POSITION if finite(cmd.position) else ControlMode.VELOCITY
        elif mode == FirmwareMode.CURRENT:
            cmd.mode = ControlMode.TORQUE
            cmd.feedforward_torque = g(R.COMMAND_Q_CURRENT, 0.0) * m.KT
        elif mode == FirmwareMode.ZERO_VELOCITY:
            cmd.mode = ControlMode.HOLD
        elif mode == FirmwareMode.BRAKE:
            cmd.mode = ControlMode.BRAKE
        elif mode == FirmwareMode.STAY_WITHIN:
            cmd.mode = ControlMode.STAY_WITHIN
            cmd.lower_bound = g(R.COMMAND_WITHIN_LOWER_BOUND, math.nan)
            cmd.upper_bound = g(R.COMMAND_WITHIN_UPPER_BOUND, math.nan)
            cmd.feedforward_torque = g(R.COMMAND_WITHIN_FEEDFORWARD_TORQUE, 0.0)
            cmd.maximum_torque = g(R.COMMAND_WITHIN_MAX_TORQUE, math.nan)
        else:
            self.console.append(f"mode {mode} not simulated - ignored")
            return
        m.apply(cmd, now)
        self.last = ReceivedCommand(firmware_mode=mode, registers=dict(writes), time=now)

    def channel_values(self, name: str) -> dict:
        m = self.motor
        now = time.monotonic()
        if name == "firmware":
            return {"version": SIM_FIRMWARE_VERSION, "model": SIM_MODEL,
                    "serial_number": [int.from_bytes(self.uuid[i:i + 4], "little") for i in (0, 4, 8)]}
        if name == "git":
            return {"hash": list(bytes.fromhex("51a0c0ffee" * 4)), "dirty": False,
                    "timestamp": 1794200000}          # fixed build stamp of the simulator image
        if name == "drv8323":
            f = m.fault == 33
            return {"fault": f, "vds_ocp": f, "gdf": False, "uvlo": m.supply_voltage < 8.0,
                    "otsd": m.temp > 120, "vds_ha": f, "vds_la": False}
        t = m.telemetry(now)
        return {"mode": t.mode, "fault": t.fault, "position": t.position, "velocity": t.velocity,
                "torque_Nm": t.torque, "q_A": t.q_current, "d_A": t.d_current, "bus_V": t.voltage,
                "filt_bus_V": m.supply_voltage, "fet_temp_C": t.temperature, "filt_fet_temp_C": m.temp,
                "motor_temp_C": math.nan, "power_W": t.power, "control_position": t.control_position,
                "control_velocity": t.control_velocity}

    def _blob(self, header: str, payload: bytes) -> bytes:
        # "<header>\r\n" then 4-byte length and payload: the moteus client
        # reads the line up to '\r', then expects '\n' + uint32 + data.
        return header.encode() + b"\r\n" + struct.pack("<I", len(payload)) + payload

    # ---- diagnostic text channel --------------------------------------
    def _exec(self, line: str) -> str | bytes:
        m = self.motor
        parts = line.split()
        if not parts:
            return ""
        self.console.append(f"> {line}")
        ok = "OK\r\n"
        try:
            if parts[:2] == ["conf", "get"] and len(parts) == 3:
                return (m.config[parts[2]] + "\r\n") if parts[2] in m.config else "ERR error reading\r\n"
            if parts[:2] == ["conf", "set"] and len(parts) == 4:
                key, val = parts[2], parts[3]
                if key not in m.config:
                    return "ERR error setting\r\n"
                float(val)
                m.config[key] = val
                if key == "id.id":
                    self.pending_id = int(float(val))
                return ok
            if parts[:2] == ["conf", "enumerate"]:
                prefix = parts[2] if len(parts) > 2 else ""
                keys = [k for k in m.config if k == prefix or k.startswith(prefix + ".") or not prefix]
                if prefix and not keys:
                    return "ERR unknown group\r\n"
                return "".join(f"{k} {m.config[k]}\r\n" for k in keys) + ok
            if parts[:2] in (["conf", "write"], ["conf", "load"]):
                return ok
            if parts[0] == "tel":
                if parts[1:2] == ["list"]:
                    return "".join(f"{c}\r\n" for c in TELEMETRY_CHANNELS) + ok
                if parts[1:2] in (["stop"], ["fmt"], ["rate"]):
                    return ok
                if parts[1:2] in (["schema"], ["get"]) and len(parts) == 3:
                    name = parts[2]
                    if name not in CHANNEL_SPECS:
                        return f"ERR unknown channel '{name}'\r\n"
                    spec = CHANNEL_SPECS[name]
                    if parts[1] == "schema":
                        return self._blob(f"schema {name}", _schema(spec))
                    return self._blob(f"emit {name}", _data(spec, self.channel_values(name)))
                return "ERR unknown tel command\r\n"
            if parts[0] == "d":
                now = time.monotonic()
                if parts[1:2] == ["stop"]:
                    m.apply(MotorCommand(mode=ControlMode.STOP), now)
                    return ok
                if parts[1:2] == ["brake"]:
                    m.apply(MotorCommand(mode=ControlMode.BRAKE), now)
                    return ok
                if parts[1:2] in (["rezero"], ["cfg-set-output"]):
                    value = float(parts[2]) if len(parts) > 2 else 0.0
                    m.offset = value - m.pos
                    m.home_state = 2
                    if parts[1] == "cfg-set-output":
                        m.config["motor_position.output.offset"] = f"{m.offset:.6f}"
                    return ok
                return f"ERR '{line}' is not simulated\r\n"
        except (ValueError, IndexError):
            return "ERR parse error\r\n"
        return "ERR unknown command\r\n"

    def _stream_in(self, channel: int, data: bytes) -> None:
        buf = self._rx_text.get(channel, b"") + data
        while True:
            idx = min((i for i in (buf.find(b"\n"), buf.find(b"\r")) if i >= 0), default=-1)
            if idx < 0:
                break
            line, buf = buf[:idx], buf[idx + 1:]
            out = self._exec(line.decode("latin1").strip())
            if out:
                raw = out if isinstance(out, bytes) else out.encode("latin1")
                self._tx_text[channel] = self._tx_text.get(channel, b"") + raw
        self._rx_text[channel] = buf[-1024:]

    # ---- frame handling ---------------------------------------------------
    def handle(self, data: bytes, now: float, reply_required: bool) -> Optional[bytes]:
        writes: dict = {}
        reads: list[tuple[int, int]] = []
        polls: list[tuple[int, int]] = []
        for sf in mp.parse_frame(data):
            if isinstance(sf, mp.RegisterSubframe):
                if sf.type == mp.SubframeType.WRITE:
                    writes[sf.register] = scale_register(sf.register, sf.resolution, sf.value)
                elif sf.type == mp.SubframeType.READ:
                    reads.append((sf.register, sf.resolution))
            elif isinstance(sf, mp.StreamSubframe):
                if sf.type == mp.SubframeType.STREAM_CLIENT_TO_SERVER:
                    self._stream_in(sf.channel, sf.data)
                elif sf.type == mp.SubframeType.STREAM_CLIENT_POLL_SERVER:
                    polls.append((sf.channel, sf.data[0] if sf.data else 48))
            # Flow-controlled polls are deliberately unanswered: the client
            # library then falls back to plain polling (as with older firmware).
        # UUID-prefix addressing: a frame carrying UUID_MASK writes is only
        # for the node whose UUID matches every supplied 32-bit word.
        masks = [r for r in (R.UUID_MASK1, R.UUID_MASK2, R.UUID_MASK3, R.UUID_MASK4) if r in writes]
        for r in masks:
            k = r - R.UUID_MASK1
            mine = int.from_bytes(self.uuid[4 * k:4 * k + 4], "little", signed=True)
            if int(writes[r]) != mine:
                return None
        for r in masks:
            writes.pop(r)
        self.frames_rx += 1
        if writes:
            self._apply_writes(writes, now)
        if not reply_required:
            return None
        buf = io.BytesIO()
        w = Writer(buf)
        # Mirror the read layout: runs of consecutive registers with the same
        # resolution become one reply subframe.
        i = 0
        while i < len(reads):
            start, res = reads[i]
            j = i + 1
            while j < len(reads) and reads[j] == (start + (j - i), res):
                j += 1
            count = j - i
            code = mp.REPLY_BASE | (res << 2)
            if count <= 3:
                w.write_int8(code | count)
            else:
                w.write_int8(code)
                w.write_varuint(count)
            w.write_varuint(start)
            for k in range(count):
                _write_value(w, start + k, self._read(start + k, now), res)
            i = j
        for ch, maxlen in polls:
            pending = self._tx_text.get(ch, b"")
            out = pending[:max(0, min(maxlen, 61))]
            self._tx_text[ch] = pending[len(out):]
            w.write_int8(mp.STREAM_SERVER_DATA)
            w.write_varuint(ch)
            w.write_varuint(len(out))
            buf.write(out)
        out = buf.getvalue()
        if not out:
            return None
        self.frames_tx += 1
        return out


class CanFdSimulator:
    """Owns the bus and a set of ``VirtualMoteus`` nodes."""

    def __init__(self, ids=(1, 2), interface: str = DEFAULT_INTERFACE,
                 channel: str = DEFAULT_CHANNEL, can_prefix: int = 0, **bus_kwargs):
        self.interface, self.channel, self.prefix = interface, channel, int(can_prefix)
        self.bus_kwargs = bus_kwargs
        self.nodes: dict[int, VirtualMoteus] = {i: VirtualMoteus(i) for i in ids}
        self.lock = threading.RLock()
        self.bus = None
        self._run = False
        self._threads: list[threading.Thread] = []
        self.stats = {"rx": 0, "tx": 0, "rx_bytes": 0, "tx_bytes": 0, "errors": 0,
                      "ignored_prefix": 0, "ignored_id": 0}
        self.last_ignored_prefix: Optional[int] = None
        self.last_ignored_id: Optional[int] = None
        self.frame_log: deque[str] = deque(maxlen=400)

    def start(self) -> None:
        import can
        kwargs = dict(self.bus_kwargs)
        kwargs.setdefault("fd", True)
        if self.interface == "udp_multicast":
            kwargs.setdefault("receive_own_messages", False)
        self.bus = can.Bus(interface=self.interface, channel=self.channel, **kwargs)
        self._run = True
        self._threads = [threading.Thread(target=self._rx_loop, name="sim-rx", daemon=True),
                         threading.Thread(target=self._physics_loop, name="sim-physics", daemon=True)]
        for t in self._threads:
            t.start()
        log.info("simulator up on %s:%s with ids %s", self.interface, self.channel, list(self.nodes))

    def stop(self) -> None:
        self._run = False
        for t in self._threads:
            t.join(1.0)
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
            self.bus = None

    def _physics_loop(self) -> None:
        last = time.monotonic()
        while self._run:
            time.sleep(0.002)
            now = time.monotonic()
            dt = min(0.02, now - last)
            last = now
            with self.lock:
                for n in self.nodes.values():
                    n.motor.step(dt, now)

    def _rx_loop(self) -> None:
        import can
        while self._run:
            try:
                msg = self.bus.recv(0.1)
            except Exception as e:  # bus error: keep running
                self.stats["errors"] += 1
                log.warning("recv: %s", e)
                continue
            if msg is None or msg.is_error_frame:
                continue
            arb = msg.arbitration_id
            dest, src, reply_req = arb & 0x7F, (arb >> 8) & 0x7F, bool(arb & 0x8000)
            if src != 0 and dest == 0:
                continue          # a controller's reply (ours or another node's), not a request
            if ((arb >> 16) & 0x1FFF) != self.prefix:
                self.stats["ignored_prefix"] += 1
                self.last_ignored_prefix = (arb >> 16) & 0x1FFF
                continue
            if dest != 0x7F and dest not in self.nodes:
                self.stats["ignored_id"] += 1
                self.last_ignored_id = dest
                continue
            data = bytes(msg.data)
            self.stats["rx"] += 1
            self.stats["rx_bytes"] += len(data)
            now = time.monotonic()
            with self.lock:
                targets = list(self.nodes.values()) if dest == 0x7F else \
                    [self.nodes[dest]] if dest in self.nodes else []
                replies = []
                for node in targets:
                    if node.motor.unresponsive:
                        continue
                    out = node.handle(data, now, reply_req)
                    new_id = getattr(node, "pending_id", None)
                    if new_id and new_id != node.id and new_id not in self.nodes:
                        self.nodes.pop(node.id)
                        node.id = node.motor.id = new_id
                        self.nodes[new_id] = node
                    if new_id:
                        node.pending_id = None
                    if out is not None:
                        replies.append((node, out))
                self.frame_log.append(f"RX {arb:08x} {data.hex(' ')}")
            for node, out in replies:
                out_id = (node.id << 8) | src | (self.prefix << 16)
                frame = pad_fd(out)
                try:
                    self.bus.send(can.Message(arbitration_id=out_id, data=frame, is_fd=True,
                                              bitrate_switch=True, is_extended_id=out_id > 0x7FF))
                    self.stats["tx"] += 1
                    self.stats["tx_bytes"] += len(frame)
                    self.frame_log.append(f"TX {out_id:08x} {frame.hex(' ')}")
                except Exception as e:
                    self.stats["errors"] += 1
                    log.warning("send: %s", e)
