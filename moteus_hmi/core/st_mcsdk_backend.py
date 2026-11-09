"""STMicroelectronics Motor Control SDK 5.x driver (e.g. X-NUCLEO-IHM08M1).

Hardware chain:  PC --USB--> ST-LINK virtual COM port --UART--> STM32 Nucleo
running MCSDK 5.x firmware --> X-NUCLEO-IHM08M1 power stage --> motor.

Protocol: MCSDK "Motor Control Protocol" / Frame Communication Protocol
(FCP), as used by ST Motor Control Workbench 5.x / Motor Pilot:

    [header][payload_len][payload ...][crc]
    header = (motor << 5) | frame_code          (motor 0 = current target)
    crc    = low byte + high byte of the 16 bit sum of all previous bytes
    reply  = 0xF0 ACK + payload, or 0xFF NACK + 1 byte error code

Register ids / frame codes follow MCSDK 5.4 (cross-checked with a public
host implementation tested on Nucleo + IHM08M1).  MCSDK 6.x replaced this
with ASPEP/MCP framing, which this driver does not speak - see
docs/ADDING_A_CONTROLLER.md.

Limitations a commissioning engineer must know:
  * No command watchdog in MCSDK: the HMI cannot make the drive stop if
    the PC/USB link dies.  A hard-wired E-stop / STO is mandatory.
  * No absolute position feedback through the protocol: speed and torque
    (Iq) control only.
  * Parameters written over the protocol are RAM only; persistent tuning
    is done by regenerating firmware in MC Workbench.
"""

from __future__ import annotations

import asyncio
import math
import struct
import threading
import time
from typing import Any, Optional

from .backend import BackendError, DiagnosticError, MotorBackend
from .types import (ControlMode, DeviceDescriptor, FirmwareMode, MotorCommand, Telemetry,
                    TransportConfig, finite)

# ---- protocol constants ----------------------------------------------
FC_SET_REG = 0x01
FC_GET_REG = 0x02
FC_EXECUTE_CMD = 0x03
FC_GET_BOARD_INFO = 0x06
FC_SET_RAMP = 0x07
FC_SET_CURRENT_REF = 0x0A
FC_ACK = 0xF0
FC_NACK = 0xFF

CMD_START_MOTOR = 0x01
CMD_STOP_MOTOR = 0x02
CMD_STOP_RAMP = 0x03
CMD_START_STOP = 0x06
CMD_FAULT_ACK = 0x07
CMD_ENCODER_ALIGN = 0x08
CMD_IQDREF_CLEAR = 0x09

NACK_ERRORS = {
    0x01: "bad frame id", 0x02: "register is read-only", 0x03: "register is write-only",
    0x05: "wrong set value", 0x07: "wrong command", 0x08: "overrun", 0x09: "timeout",
    0x0A: "bad CRC", 0x0B: "bad motor selected", 0x0C: "motor profiler not enabled",
}

# name: (id, struct format, writable, description)
REGISTERS: dict[str, tuple[int, str, bool, str]] = {
    "target_motor": (0x00, "B", True, "Target motor index"),
    "fault_flags": (0x01, "I", False, "Faults: now<<16 | occurred"),
    "status": (0x02, "B", False, "State machine state"),
    "control_mode": (0x03, "B", True, "0 = torque, 1 = speed"),
    "speed_ref": (0x04, "i", False, "Speed reference [rpm]"),
    "speed_kp": (0x05, "h", True, "Speed loop Kp"),
    "speed_ki": (0x06, "h", True, "Speed loop Ki"),
    "speed_kd": (0x07, "h", True, "Speed loop Kd"),
    "torque_ref": (0x08, "h", True, "Iq reference [digit]"),
    "torque_kp": (0x09, "h", True, "Iq loop Kp"),
    "torque_ki": (0x0A, "h", True, "Iq loop Ki"),
    "torque_kd": (0x0B, "h", True, "Iq loop Kd"),
    "flux_ref": (0x0C, "h", True, "Id reference [digit]"),
    "flux_kp": (0x0D, "h", True, "Id loop Kp"),
    "flux_ki": (0x0E, "h", True, "Id loop Ki"),
    "flux_kd": (0x0F, "h", True, "Id loop Kd"),
    "bus_voltage": (0x19, "H", False, "DC bus voltage [V]"),
    "heatsink_temp": (0x1A, "H", False, "Heatsink temperature [°C]"),
    "motor_power": (0x1B, "H", False, "Motor power [W]"),
    "speed_meas": (0x1E, "i", False, "Measured speed [rpm]"),
    "torque_meas": (0x1F, "h", False, "Measured Iq [digit]"),
    "flux_meas": (0x20, "h", False, "Measured Id [digit]"),
    "max_app_speed": (0x3F, "I", False, "Max application speed [rpm]"),
    "min_app_speed": (0x40, "I", False, "Min application speed [rpm]"),
    "iq_speed_mode": (0x41, "h", False, "Iq ref in speed mode [digit]"),
    "ramp_final_speed": (0x5B, "i", True, "Ramp final speed [rpm]"),
    "speed_kp_div": (0x6E, "h", False, "Speed Kp divisor"),
    "speed_ki_div": (0x6F, "h", False, "Speed Ki divisor"),
    "ctrl_board_id": (0x72, "I", False, "Control board id"),
    "power_board_id": (0x73, "I", False, "Power board id"),
}
TUNABLE = ("speed_kp", "speed_ki", "speed_kd", "torque_kp", "torque_ki", "torque_kd",
           "flux_kp", "flux_ki", "flux_kd")

STATES = {
    0: "Idle", 1: "Idle alignment", 2: "Alignment", 3: "Idle start", 4: "Start",
    5: "Start run", 6: "Run", 7: "Any stop", 8: "Stop", 9: "Stop idle",
    10: "Fault now", 11: "Fault over", 12: "ICL wait", 13: "Align charge boot cap",
    14: "Align offset calib", 15: "Align clear", 16: "Charge boot cap",
    17: "Offset calib", 18: "Clear", 19: "Switch over", 20: "Wait stop motor",
}
IDLE_STATES = {0, 9, 12}
FAULT_STATES = {10, 11}
RUN_STATES = {3, 4, 5, 6, 16, 17, 18, 19}

FAULT_BITS = {
    0x0001: "FOC duration (control loop overrun)",
    0x0002: "Over voltage",
    0x0004: "Under voltage",
    0x0008: "Over temperature",
    0x0010: "Start-up failure",
    0x0020: "Speed feedback",
    0x0040: "Over current",
    0x0080: "Software error",
}


def describe_faults(mask: int) -> str:
    names = [n for b, n in FAULT_BITS.items() if mask & b]
    unknown = mask & ~sum(FAULT_BITS)
    if unknown:
        names.append(f"0x{unknown:04x}")
    return ", ".join(names)


def fcp_crc(data: bytes) -> int:
    acc = sum(data) & 0xFFFF
    return ((acc & 0xFF) + (acc >> 8)) & 0xFF


def build_frame(code: int, payload: bytes = b"", motor: int = 0) -> bytes:
    if len(payload) > 255:
        raise ValueError("payload too long")
    head = bytes([((motor & 0x7) << 5) | (code & 0x1F), len(payload)]) + payload
    return head + bytes([fcp_crc(head)])


class FcpNack(DiagnosticError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"ERR NACK 0x{code:02x}: {NACK_ERRORS.get(code, 'unknown error')}")


class FcpLink:
    """Blocking request/response over a pyserial-like port (thread-safe)."""

    def __init__(self, port: Any):
        self.port = port
        self._lock = threading.Lock()
        self.tx_frames = 0
        self.errors = 0
        self.last_tx = ""

    @classmethod
    def open(cls, device: str, baudrate: int) -> "FcpLink":
        import serial
        p = serial.Serial(device, baudrate=baudrate, bytesize=8, parity="N", stopbits=1,
                          timeout=0.1, write_timeout=0.1)
        return cls(p)

    def close(self) -> None:
        try:
            self.port.close()
        except Exception:
            pass

    def _read_exact(self, n: int, deadline: float) -> bytes:
        buf = b""
        while len(buf) < n:
            if time.monotonic() > deadline:
                raise TimeoutError("FCP reply timeout")
            chunk = self.port.read(n - len(buf))
            if chunk:
                buf += chunk
        return buf

    def transact(self, code: int, payload: bytes = b"", timeout: float = 0.15) -> bytes:
        with self._lock:
            try:
                self.port.reset_input_buffer()
            except Exception:
                pass
            frame = build_frame(code, payload)
            self.last_tx = frame.hex(" ").upper()
            self.port.write(frame)
            self.tx_frames += 1
            deadline = time.monotonic() + timeout
            head = self._read_exact(2, deadline)
            body = self._read_exact(head[1] + 1, deadline)
            frame, crc = head + body[:-1], body[-1]
            if fcp_crc(frame) != crc:
                self.errors += 1
                raise BackendError("FCP CRC mismatch in reply")
            if head[0] == FC_NACK:
                self.errors += 1
                raise FcpNack(body[0] if len(body) > 1 else 0)
            if head[0] != FC_ACK:
                self.errors += 1
                raise BackendError(f"Unexpected FCP reply header 0x{head[0]:02x}")
            return bytes(body[:-1])

    # Convenience wrappers ---------------------------------------------
    def get_reg(self, name: str) -> int:
        rid, fmt, _, _ = REGISTERS[name]
        data = self.transact(FC_GET_REG, bytes([rid]))
        size = struct.calcsize("<" + fmt)
        if len(data) < size:
            raise BackendError(f"Short reply for register {name}")
        return struct.unpack("<" + fmt, data[:size])[0]

    def set_reg(self, name: str, value: int) -> None:
        rid, fmt, writable, _ = REGISTERS[name]
        if not writable:
            raise DiagnosticError(f"ERR register {name} is read-only")
        self.transact(FC_SET_REG, bytes([rid]) + struct.pack("<" + fmt, int(value)))

    def execute(self, cmd: int) -> None:
        self.transact(FC_EXECUTE_CMD, bytes([cmd]))

    def set_ramp(self, final_rpm: int, duration_ms: int) -> None:
        self.transact(FC_SET_RAMP, struct.pack("<iH", int(final_rpm), max(0, min(65535, int(duration_ms)))))

    def board_info(self) -> str:
        data = self.transact(FC_GET_BOARD_INFO, b"", timeout=0.3)
        return data.decode("latin1", errors="replace").strip("\x00 ").strip()


class StMcsdkBackend(MotorBackend):
    MOTOR_ID = 1

    def __init__(self, config: TransportConfig, profile):
        super().__init__(config)
        self.profile = profile
        p = profile.params
        self.amps_per_digit = config.amps_per_digit or p.get("amps_per_digit", 0.001)
        self.kt = config.torque_constant or p.get("torque_constant_Nm_per_A", 0.05)
        self.link: Optional[FcpLink] = None
        self._cycle = 0
        self._slow: dict[str, float] = {"voltage": math.nan, "temperature": math.nan, "power": math.nan}
        self._mode_set: Optional[int] = None
        self._last_target: Optional[tuple] = None
        self._state = 0
        self._board = ""
        self._last_tx = ""

    async def _io(self, fn, *args):
        if self.link is None:
            raise BackendError("Serial link not open")
        return await asyncio.to_thread(fn, *args)

    async def open(self) -> str:
        port = self.config.serial_port
        if not port:
            raise BackendError("Select the Nucleo ST-LINK serial port")
        try:
            self.link = await asyncio.to_thread(FcpLink.open, port, int(self.config.baudrate or 115200))
        except Exception as e:
            raise BackendError(f"Cannot open {port}: {e}") from e
        try:
            self._board = await self._io(self.link.board_info)
        except Exception:
            self._board = ""
        return f"MCSDK on {port}" + (f" ({self._board})" if self._board else "")

    async def discover(self) -> list[DeviceDescriptor]:
        try:
            await self._io(self.link.get_reg, "status")
        except Exception as e:
            raise BackendError(f"No MCSDK reply on {self.config.serial_port}: {e}. Check the "
                               "baud rate and that the firmware was generated with the UART "
                               "motor control protocol enabled.") from e
        return [DeviceDescriptor(motor_id=self.MOTOR_ID, uuid=self._board,
                                 bus=self.config.serial_port, address=0)]

    def attach(self, devices):
        self.devices = {d.motor_id: d for d in devices}

    # ---- command application ----------------------------------------
    async def _ensure_mode(self, mode: int) -> None:
        if self._mode_set != mode:
            await self._io(self.link.set_reg, "control_mode", mode)
            self._mode_set = mode
            self._last_target = None

    async def _apply(self, cmd: MotorCommand, speed_rpm: float) -> None:
        m = cmd.mode
        st = self._state
        if m == ControlMode.QUERY:
            return
        if m in (ControlMode.STOP, ControlMode.HOLD, ControlMode.BRAKE):
            if st in FAULT_STATES and m == ControlMode.STOP:
                await self._io(self.link.execute, CMD_FAULT_ACK)
            elif st not in IDLE_STATES and st not in FAULT_STATES and st != 8:
                await self._io(self.link.execute, CMD_STOP_MOTOR)
            self._last_target = None
            return
        if st in FAULT_STATES:
            return
        if m == ControlMode.VELOCITY:
            await self._ensure_mode(1)
            rpm = int(round((cmd.velocity if finite(cmd.velocity) else 0.0) * 60.0))
            accel = cmd.accel_limit if finite(cmd.accel_limit) and cmd.accel_limit > 0 else 10.0
            target = ("v", rpm)
            if target != self._last_target:
                cur = speed_rpm if math.isfinite(speed_rpm) else 0.0
                duration = abs(rpm - cur) / (accel * 60.0) * 1000.0
                await self._io(self.link.set_ramp, rpm, int(duration))
                self._last_target = target
        elif m == ControlMode.TORQUE:
            await self._ensure_mode(0)
            amps = (cmd.feedforward_torque if finite(cmd.feedforward_torque) else 0.0) / self.kt
            digits = int(max(-32767, min(32767, round(amps / self.amps_per_digit))))
            target = ("t", digits)
            if target != self._last_target:
                await self._io(self.link.set_reg, "torque_ref", digits)
                self._last_target = target
        else:
            raise BackendError(f"{m.label} is not supported by MCSDK")
        if st in IDLE_STATES:
            await self._io(self.link.execute, CMD_START_MOTOR)

    async def cycle(self, commands, timeout_s):
        out: dict[int, Optional[Telemetry]] = {self.MOTOR_ID: None}
        if self.link is None:
            return out
        try:
            status = await self._io(self.link.get_reg, "status")
            self._state = int(status)
            speed = await self._io(self.link.get_reg, "speed_meas")
            await self._apply(commands.get(self.MOTOR_ID, MotorCommand()), float(speed))
            faults = await self._io(self.link.get_reg, "fault_flags")
            iq = await self._io(self.link.get_reg, "torque_meas")
            idd = await self._io(self.link.get_reg, "flux_meas")
            if self._cycle % 5 == 0:
                self._slow["voltage"] = float(await self._io(self.link.get_reg, "bus_voltage"))
                self._slow["temperature"] = float(await self._io(self.link.get_reg, "heatsink_temp"))
                self._slow["power"] = float(await self._io(self.link.get_reg, "motor_power"))
            self._cycle += 1
        except (BackendError, TimeoutError, OSError) as e:
            if isinstance(e, FcpNack):
                raise
            return out
        now_faults, past_faults = (faults >> 16) & 0xFFFF, faults & 0xFFFF
        st = self._state
        in_fault = st in FAULT_STATES
        fmask = now_faults or (past_faults if in_fault else 0)
        iq_a, id_a = iq * self.amps_per_digit, idd * self.amps_per_digit
        mode = (FirmwareMode.FAULT if in_fault else
                FirmwareMode.STOPPED if st in IDLE_STATES or st == 8 else FirmwareMode.POSITION)
        ctrl = "speed" if self._mode_set == 1 else "torque" if self._mode_set == 0 else ""
        out[self.MOTOR_ID] = Telemetry(
            motor_id=self.MOTOR_ID, timestamp=time.monotonic(), mode=int(mode),
            position=math.nan, velocity=speed / 60.0, torque=iq_a * self.kt,
            q_current=iq_a, d_current=id_a, power=self._slow["power"],
            temperature=self._slow["temperature"], voltage=self._slow["voltage"],
            fault=int(fmask), home_state=0,
            state_text=STATES.get(st, f"State {st}") + (f" ({ctrl})" if ctrl and st in RUN_STATES else ""),
            fault_text=describe_faults(fmask) if fmask else "")
        return out

    def last_frames(self):
        self._last_tx = self.link.last_tx if self.link else ""
        n = self.link.tx_frames if self.link else 0
        d, self._tx_seen = n - getattr(self, "_tx_seen", 0), n
        return ({1: self._last_tx}, {1: f"state {STATES.get(self._state, '?')}"}, d * 4, d * 6)

    async def stop_all(self):
        if self.link is None:
            return
        try:
            await self._io(self.link.execute, CMD_STOP_MOTOR)
        except Exception:
            pass
        self._last_target = None

    # ---- engineering access -------------------------------------------
    async def diagnostic(self, motor_id, command, allow_any_response=False, timeout_s=5.0):
        parts = command.strip().split()
        if not parts:
            raise DiagnosticError("ERR empty command")
        key = lambda n: f"mcsdk.{n}"
        if parts[:2] == ["conf", "enumerate"]:
            lines = []
            for name in REGISTERS:
                try:
                    lines.append(f"{key(name)} {await self._io(self.link.get_reg, name)}")
                except DiagnosticError:
                    continue
            return "\n".join(lines) + "\n"
        if parts[:2] == ["conf", "get"] and len(parts) == 3:
            name = parts[2].removeprefix("mcsdk.")
            if name not in REGISTERS:
                raise DiagnosticError("ERR error reading")
            return str(await self._io(self.link.get_reg, name))
        if parts[:2] == ["conf", "set"] and len(parts) == 4:
            name = parts[2].removeprefix("mcsdk.")
            if name not in TUNABLE:
                raise DiagnosticError(f"ERR {name} is not a tunable MCSDK register")
            await self._io(self.link.set_reg, name, int(float(parts[3])))
            return ""
        if parts[:2] == ["conf", "write"]:
            raise DiagnosticError("ERR MCSDK 5.x has no persistent parameter storage - "
                                  "regenerate the firmware in MC Workbench to make tuning permanent")
        if parts[:2] == ["d", "stop"]:
            await self.stop_all()
            return ""
        if parts[0] == "cmd" and len(parts) == 2:
            cmds = {"start": CMD_START_MOTOR, "stop": CMD_STOP_MOTOR, "stop_ramp": CMD_STOP_RAMP,
                    "fault_ack": CMD_FAULT_ACK, "encoder_align": CMD_ENCODER_ALIGN,
                    "iqdref_clear": CMD_IQDREF_CLEAR}
            if parts[1] not in cmds:
                raise DiagnosticError("ERR commands: " + ", ".join(cmds))
            await self._io(self.link.execute, cmds[parts[1]])
            return ""
        if parts[0] == "info":
            return self._board or "(no board info)"
        raise DiagnosticError("ERR supported: conf enumerate | conf get K | conf set K V | "
                              "cmd <start|stop|fault_ack|encoder_align> | info")

    async def read_channel(self, motor_id, name, timeout_s=5.0):
        if name in ("registers", "servo_stats"):
            out = {}
            for reg in REGISTERS:
                try:
                    out[reg] = await self._io(self.link.get_reg, reg)
                except DiagnosticError as e:
                    out[reg] = str(e)
            out["state_name"] = STATES.get(out.get("status", -1), "?")
            return out
        if name in ("board_info", "firmware"):
            return {"board_info": self._board}
        raise DiagnosticError("ERR channels: registers, board_info")

    async def set_output(self, motor_id, position, exact):
        raise BackendError("MCSDK does not expose output position re-referencing")

    async def encoder_align(self) -> None:
        await self._io(self.link.execute, CMD_ENCODER_ALIGN)

    async def close(self):
        await self.stop_all()
        if self.link:
            await asyncio.to_thread(self.link.close)
        self.link = None


class FakeMcsdkPort:
    """In-memory MCSDK 5.x responder used by unit tests (and handy for
    checking a host implementation without a Nucleo)."""

    def __init__(self):
        self.regs = {name: 0 for name in REGISTERS}
        self.regs.update(bus_voltage=24, heatsink_temp=31, status=0, speed_kp=1500, speed_ki=100,
                         torque_kp=2000, torque_ki=300, control_mode=1)
        self._out = b""
        self.log: list[bytes] = []
        self.ramps: list[tuple[int, int]] = []

    def reset_input_buffer(self):
        self._out = b""

    def read(self, n):
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk

    def close(self):
        pass

    def _reply(self, payload: bytes = b"", nack: Optional[int] = None):
        if nack is not None:
            head = bytes([FC_NACK, 1, nack])
        else:
            head = bytes([FC_ACK, len(payload)]) + payload
        self._out += head + bytes([fcp_crc(head)])

    def write(self, frame: bytes):
        self.log.append(bytes(frame))
        if fcp_crc(frame[:-1]) != frame[-1]:
            return self._reply(nack=0x0A)
        code, payload = frame[0] & 0x1F, frame[2:-1]
        by_id = {v[0]: k for k, v in REGISTERS.items()}
        if code == FC_GET_REG:
            name = by_id.get(payload[0])
            if name is None:
                return self._reply(nack=0x01)
            return self._reply(struct.pack("<" + REGISTERS[name][1], self.regs[name]))
        if code == FC_SET_REG:
            name = by_id.get(payload[0])
            if name is None or not REGISTERS[name][2]:
                return self._reply(nack=0x02)
            self.regs[name] = struct.unpack("<" + REGISTERS[name][1], payload[1:])[0]
            return self._reply()
        if code == FC_EXECUTE_CMD:
            c = payload[0]
            if c == CMD_START_MOTOR:
                self.regs["status"] = 6
            elif c == CMD_STOP_MOTOR:
                self.regs["status"] = 0
                self.regs["speed_meas"] = 0
            elif c == CMD_FAULT_ACK:
                self.regs["status"] = 0
                self.regs["fault_flags"] = 0
            return self._reply()
        if code == FC_SET_RAMP:
            rpm, ms = struct.unpack("<iH", payload)
            self.ramps.append((rpm, ms))
            self.regs["speed_ref"] = rpm
            self.regs["speed_meas"] = rpm
            return self._reply()
        if code == FC_GET_BOARD_INFO:
            return self._reply(b"FAKE MCSDK 5.4 IHM08M1")
        return self._reply(nack=0x01)
