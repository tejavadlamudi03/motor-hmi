"""Physics model of a moteus-controlled motor, used by the Motor Simulator.

Emulates the moteus semantics: position mode with trajectory velocity /
acceleration limits, ``position = NaN`` velocity mode, kp/kd scaling,
maximum torque, zero-velocity, brake, stay-within, the command watchdog ->
TIMEOUT mode, a thermal model with over-temperature fault, supply
voltage faults, a ``conf`` parameter store and fault injection.

Sensor noise is deliberately realistic (quantisation, white noise,
occasional spikes and a 17 Hz interference tone on current) so the HMI's
noise-reduction filters can be evaluated without hardware.
"""

from __future__ import annotations

import math
import random
import time

from ..core.types import ControlMode, FirmwareMode, MotorCommand, Telemetry, finite

DEFAULT_CONFIG: dict[str, str] = {
    "id.id": "1",
    "can.prefix": "0",
    "servopos.position_min": "nan",
    "servopos.position_max": "nan",
    "servo.pid_position.kp": "4.000000",
    "servo.pid_position.ki": "1.000000",
    "servo.pid_position.kd": "0.050000",
    "servo.pid_position.iratelimit": "-1.000000",
    "servo.pid_position.ilimit": "0.000000",
    "servo.pid_position.max_desired_rate": "0.000000",
    "servo.pid_dq_hz": "200.000000",
    "servo.max_current_desired_rate": "10000.000000",
    "servo.default_velocity_limit": "nan",
    "servo.default_accel_limit": "nan",
    "servo.max_current_A": "20.000000",
    "servo.max_voltage": "46.000000",
    "servo.max_power_W": "450.000000",
    "servo.max_velocity": "500.000000",
    "servo.default_timeout_s": "0.100000",
    "servo.timeout_max_torque_Nm": "5.000000",
    "servo.timeout_mode": "12",
    "servo.fault_temperature": "75.000000",
    "servo.enable_motor_temperature": "0",
    "servo.fault_position_error": "nan",
    "servo.fault_velocity_error": "nan",
    "servo.pwm_rate_hz": "30000",
    "motor.poles": "14",
    "motor.Kv": "100.000000",
    "motor.resistance_ohm": "0.120000",
    "motor_position.commutation_source": "0",
    "motor_position.output.source": "0",
    "motor_position.output.offset": "0.000000",
    "motor_position.output.sign": "1",
    "motor_position.rotor_to_output_ratio": "1.000000",
    "motor_position.sources.0.type": "1",
    "motor_position.sources.0.cpr": "16384",
    "motor_position.sources.0.pll_filter_hz": "400.000000",
}


class SimMotor:
    # Plant (output side).
    J = 0.002          # N*m per (rev/s^2)
    B = 0.004          # viscous N*m per rev/s
    COULOMB = 0.01     # N*m
    KT = 0.08          # N*m per A
    R = 0.12           # ohm
    T_AMB = 25.0
    R_TH = 6.0         # degC per W
    C_TH = 15.0        # J per degC
    BOARD_TORQUE = 2.5  # N*m hardware ceiling

    def __init__(self, motor_id: int, seed: int = 0):
        self.id = motor_id
        self.rng = random.Random(seed or motor_id * 7919)
        self.config = dict(DEFAULT_CONFIG)
        self.config["id.id"] = str(motor_id)
        self.pos = self.rng.uniform(-0.2, 0.2)
        self.vel = 0.0
        self.temp = self.T_AMB + 3.0
        self.offset = 0.0
        self.mode = FirmwareMode.STOPPED
        self.fault = 0
        self.cmd = MotorCommand(mode=ControlMode.STOP)
        self.last_cmd_time = time.monotonic()
        self.ctrl_pos = self.pos
        self.ctrl_vel = 0.0
        self.torque = 0.0
        self.integral = 0.0
        self.home_state = 1
        self.load_torque = 0.0       # external disturbance, settable in tests
        self.unresponsive = False    # simulate a dead node / cable
        self.calibrated = True
        # Fault injection (Motor Simulator app / tests)
        self.noise_scale = 1.0
        self.supply_voltage = 24.0
        self.injected_fault = 0      # moteus fault code forced while non-zero

    def cfg(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except ValueError:
            return default

    # ---- command handling ------------------------------------------
    def apply(self, cmd: MotorCommand, now: float) -> None:
        m = cmd.mode
        if m == ControlMode.QUERY:
            return
        self.last_cmd_time = now
        if m == ControlMode.STOP:
            self.mode = FirmwareMode.STOPPED
            self.fault = 0
            self.cmd = cmd
            return
        if self.mode in (FirmwareMode.FAULT, FirmwareMode.TIMEOUT):
            return  # latched until a stop is received, like moteus
        if not self.calibrated and m != ControlMode.BRAKE:
            self.mode, self.fault = FirmwareMode.FAULT, 36
            return
        newmode = {
            ControlMode.POSITION: FirmwareMode.POSITION,
            ControlMode.VELOCITY: FirmwareMode.POSITION,
            ControlMode.TORQUE: FirmwareMode.POSITION,
            ControlMode.HOLD: FirmwareMode.ZERO_VELOCITY,
            ControlMode.BRAKE: FirmwareMode.BRAKE,
            ControlMode.STAY_WITHIN: FirmwareMode.STAY_WITHIN,
        }[m]
        if newmode == FirmwareMode.POSITION and self.mode != FirmwareMode.POSITION:
            self.ctrl_pos, self.ctrl_vel, self.integral = self.pos, self.vel, 0.0
        if newmode == FirmwareMode.POSITION:
            pmin = self.cfg("servopos.position_min", math.nan)
            pmax = self.cfg("servopos.position_max", math.nan)
            out = self.pos + self.offset
            if (math.isfinite(pmin) and out < pmin) or (math.isfinite(pmax) and out > pmax):
                self.mode, self.fault = FirmwareMode.FAULT, 39
                return
        self.mode = newmode
        # Commands arrive in output coordinates; the plant runs in rotor
        # coordinates, so remove the output offset once here.
        o = self.offset
        self.cmd = cmd.copy(
            position=cmd.position - o if finite(cmd.position) else cmd.position,
            lower_bound=cmd.lower_bound - o if finite(cmd.lower_bound) else cmd.lower_bound,
            upper_bound=cmd.upper_bound - o if finite(cmd.upper_bound) else cmd.upper_bound)

    # ---- physics -------------------------------------------------------
    def _trajectory(self, dt: float) -> None:
        c = self.cmd
        vlim = c.velocity_limit if finite(c.velocity_limit) else self.cfg("servo.default_velocity_limit", math.nan)
        alim = c.accel_limit if finite(c.accel_limit) else self.cfg("servo.default_accel_limit", math.nan)
        v_cmd = c.velocity if finite(c.velocity) else 0.0
        if c.mode == ControlMode.TORQUE:
            self.ctrl_pos, self.ctrl_vel = self.pos, self.vel
            return
        if not finite(c.position):           # velocity mode
            target_v = v_cmd
        else:
            if not finite(vlim) and not finite(alim):
                self.ctrl_pos, self.ctrl_vel = c.position, v_cmd
                return
            err = c.position - self.ctrl_pos
            a = alim if finite(alim) else 1e6
            v_max = vlim if finite(vlim) else 1e6
            target_v = math.copysign(min(v_max, math.sqrt(2 * a * abs(err))), err) + v_cmd
            if abs(err) < 1e-5 and abs(self.ctrl_vel - v_cmd) < a * dt:
                self.ctrl_pos, self.ctrl_vel = c.position, v_cmd
                return
        if finite(vlim):
            target_v = max(-vlim, min(vlim, target_v))
        if finite(alim):
            dv = max(-alim * dt, min(alim * dt, target_v - self.ctrl_vel))
            self.ctrl_vel += dv
        else:
            self.ctrl_vel = target_v
        self.ctrl_pos += self.ctrl_vel * dt

    def _control_torque(self, dt: float) -> float:
        c = self.cmd
        kp = self.cfg("servo.pid_position.kp", 4.0)
        ki = self.cfg("servo.pid_position.ki", 0.0)
        kd = self.cfg("servo.pid_position.kd", 0.05)
        if self.mode == FirmwareMode.STOPPED or self.mode == FirmwareMode.FAULT:
            return 0.0
        if self.mode == FirmwareMode.BRAKE:
            return -0.08 * self.vel
        if self.mode in (FirmwareMode.ZERO_VELOCITY, FirmwareMode.TIMEOUT):
            kds = c.kd_scale if finite(c.kd_scale) else 1.0
            t = -max(kd * kds, 0.02) * self.vel * 4.0
            lim = self.cfg("servo.timeout_max_torque_Nm", 5.0)
            return max(-lim, min(lim, t))
        if self.mode == FirmwareMode.STAY_WITHIN:
            ff = c.feedforward_torque if finite(c.feedforward_torque) else 0.0
            target = None
            if finite(c.lower_bound) and self.pos < c.lower_bound:
                target = c.lower_bound
            elif finite(c.upper_bound) and self.pos > c.upper_bound:
                target = c.upper_bound
            t = ff if target is None else kp * (target - self.pos) - kd * self.vel + ff
        else:
            self._trajectory(dt)
            kps = c.kp_scale if finite(c.kp_scale) else 1.0
            kds = c.kd_scale if finite(c.kd_scale) else 1.0
            perr = self.ctrl_pos - self.pos
            self.integral += ki * kps * perr * dt
            self.integral = max(-0.2, min(0.2, self.integral))
            ff = c.feedforward_torque if finite(c.feedforward_torque) else 0.0
            t = kp * kps * perr + kd * kds * (self.ctrl_vel - self.vel) + self.integral * (kps > 0) + ff
        tmax = c.maximum_torque if finite(c.maximum_torque) else self.BOARD_TORQUE
        tmax = min(tmax, self.BOARD_TORQUE, self.cfg("servo.max_current_A", 20.0) * self.KT)
        return max(-tmax, min(tmax, t))

    def step(self, dt: float, now: float) -> None:
        timeout = self.cmd.watchdog_timeout
        if not finite(timeout):
            timeout = self.cfg("servo.default_timeout_s", math.nan)
        if (self.mode in (FirmwareMode.POSITION, FirmwareMode.STAY_WITHIN)
                and finite(timeout) and now - self.last_cmd_time > timeout):
            self.mode = FirmwareMode.TIMEOUT
        sub = 0.0005
        n = max(1, int(dt / sub))
        h = dt / n
        for _ in range(n):
            self.torque = self._control_torque(h)
            friction = self.COULOMB * math.tanh(self.vel / 0.01)
            acc = (self.torque - self.B * self.vel - friction + self.load_torque) / self.J
            self.vel += acc * h
            self.pos += self.vel * h
        current = self.torque / self.KT
        p_loss = self.R * current ** 2 + 0.3
        self.temp += (p_loss - (self.temp - self.T_AMB) / self.R_TH) / self.C_TH * dt
        if self.mode != FirmwareMode.FAULT:
            if self.injected_fault:
                self.mode, self.fault = FirmwareMode.FAULT, int(self.injected_fault)
            elif self.temp > self.cfg("servo.fault_temperature", 75.0):
                self.mode, self.fault = FirmwareMode.FAULT, 38
            elif self.supply_voltage > self.cfg("servo.max_voltage", 46.0):
                self.mode, self.fault = FirmwareMode.FAULT, 34
            elif self.supply_voltage < 8.0 and self.mode != FirmwareMode.STOPPED:
                self.mode, self.fault = FirmwareMode.FAULT, 40

    def telemetry(self, now: float) -> Telemetry:
        ns = self.noise_scale
        g = lambda mu, sigma: self.rng.gauss(mu, sigma * ns)
        cpr = self.cfg("motor_position.sources.0.cpr", 16384)
        pos_meas = round((self.pos + self.offset) * cpr) / cpr + g(0, 1.5e-5)
        vel_meas = self.vel + g(0, 0.015)
        if ns > 0 and self.rng.random() < 0.01:
            vel_meas += self.rng.choice((-1, 1)) * self.rng.uniform(0.3, 0.8) * ns  # spike
        ripple = 0.08 * ns * math.sin(2 * math.pi * 17.0 * now)
        iq = self.torque / self.KT + ripple + g(0, 0.05)
        if ns > 0 and self.rng.random() < 0.005:
            iq += self.rng.choice((-1, 1)) * 3.0 * ns
        volts = self.supply_voltage - 0.02 * abs(iq) + g(0, 0.05)
        power = self.torque * self.vel * 2 * math.pi + self.R * iq * iq
        in_ctrl = self.mode == FirmwareMode.POSITION
        return Telemetry(
            motor_id=self.id, timestamp=now, mode=int(self.mode),
            position=pos_meas, velocity=vel_meas,
            torque=self.torque + g(0, 0.004), q_current=iq, d_current=g(0, 0.04),
            power=power, motor_temperature=math.nan,
            temperature=self.temp + g(0, 0.1), voltage=volts, fault=self.fault,
            trajectory_complete=in_ctrl and abs(self.ctrl_pos - (self.cmd.position if finite(self.cmd.position) else self.ctrl_pos)) < 1e-4,
            home_state=self.home_state,
            control_position=(self.ctrl_pos + self.offset) if in_ctrl else math.nan,
            control_velocity=self.ctrl_vel if in_ctrl else math.nan,
        )
