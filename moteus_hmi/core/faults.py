"""moteus fault code (register 0x00f) descriptions.

Source: docs/protocol/registers.md in github.com/mjbots/moteus.
Codes >= 96 are *limit* indicators reported during normal operation,
not faults.
"""

from __future__ import annotations

FAULTS: dict[int, tuple[str, str]] = {
    32: ("Calibration fault",
         "The encoder could not sense a magnet during calibration."),
    33: ("Motor driver fault",
         "Usually under-voltage: the supply could not provide the requested "
         "current. Inspect the drv8323 diagnostic channel for details."),
    34: ("Over voltage",
         "Bus voltage exceeded servo.max_voltage. Often caused by regeneration "
         "into a supply that cannot sink power; configure flux braking."),
    35: ("Encoder fault", "Encoder readings are inconsistent with a magnet."),
    36: ("Motor not configured", "Run the calibration procedure first."),
    37: ("PWM cycle overrun", "Internal firmware error."),
    38: ("Over temperature", "Maximum configured temperature exceeded."),
    39: ("Outside limit",
         "Position control started outside servopos.position_min/max."),
    40: ("Under voltage", "Bus voltage too low."),
    41: ("Config changed", "A configuration value requiring a stop was changed."),
    42: ("Theta invalid", "No valid commutation encoder is available."),
    43: ("Position invalid", "No valid output encoder is available."),
    44: ("Driver enable fault", "The MOSFET gate driver could not be enabled."),
    45: ("Stop position deprecated",
         "Stop position used together with velocity/accel limits."),
    46: ("Timing violation", "Internal timing constraint violated."),
    47: ("BEMF feedforward without accel",
         "servo.bemf_feedforward set but no acceleration limit was given."),
    48: ("Invalid limits",
         "servopos.position_min/max outside the available position range."),
    49: ("Position control error",
         "Position error exceeded servo.fault_position_error."),
    50: ("Velocity control error",
         "Velocity error exceeded servo.fault_velocity_error."),
}

LIMITS: dict[int, str] = {
    96: "Limited by servo.max_velocity",
    97: "Limited by servo.max_power_W",
    98: "Limited by maximum system voltage",
    99: "Limited by servo.max_current_A",
    100: "Limited by servo.fault_temperature",
    101: "Limited by servo.motor_fault_temperature",
    102: "Limited by commanded maximum torque",
    103: "Limited by servopos.position_min/max",
}


def is_limit_code(code: int) -> bool:
    return int(code) in LIMITS


def fault_title(code: int) -> str:
    code = int(code)
    if code == 0:
        return "None"
    if code in FAULTS:
        return f"{code}: {FAULTS[code][0]}"
    if code in LIMITS:
        return f"{code}: {LIMITS[code]}"
    return f"{code}: Unknown"


def fault_help(code: int) -> str:
    code = int(code)
    if code in FAULTS:
        return FAULTS[code][1]
    if code in LIMITS:
        return "Informational: output is being limited, the controller is not faulted."
    return ""
