"""Controller registry - the "Controller" drop-down in the connection dialog.

Each profile states which driver talks to it, which links (adapters) it
supports, which operator modes / engineering features it offers, and its
electrical envelope (used to bound the safety limits).

Adding a new controller = add a ``ControllerProfile`` here and, if it
speaks a new protocol, a ``MotorBackend`` subclass registered in
``make_backend``.  See docs/ADDING_A_CONTROLLER.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import ControlMode, TransportConfig

# Feature flags
F_POSITION = "position"              # absolute output position available
F_FW_WATCHDOG = "firmware_watchdog"  # controller stops by itself if commands stop
F_CONF = "conf"                      # parameter read/write
F_CONF_PERSIST = "conf_persist"      # parameters can be saved to flash
F_CAL_TOOL = "moteus_calibration"    # full moteus_tool --calibrate
F_ENCODER_ALIGN = "encoder_align"    # ST MCSDK encoder alignment command
F_SET_OUTPUT = "set_output"          # re-reference output position
F_CHANNELS = "channels"              # binary telemetry channels
F_DISCOVERY = "discovery"            # bus scan for several nodes
F_CONSOLE = "console"                # text diagnostic console
F_STEP_TEST = "step_test"            # position step response

MOTEUS_LINKS = ("auto", "fdcanusb", "socketcan", "pythoncan", "virtual_canfd")
MOTEUS_MODES = (ControlMode.POSITION, ControlMode.VELOCITY, ControlMode.TORQUE,
                ControlMode.HOLD, ControlMode.BRAKE, ControlMode.STAY_WITHIN)
MOTEUS_FEATURES = frozenset({F_POSITION, F_FW_WATCHDOG, F_CONF, F_CONF_PERSIST, F_CAL_TOOL,
                             F_SET_OUTPUT, F_CHANNELS, F_DISCOVERY, F_CONSOLE, F_STEP_TEST})

ST_LINKS = ("st_serial",)
ST_MODES = (ControlMode.VELOCITY, ControlMode.TORQUE)
ST_FEATURES = frozenset({F_CONF, F_ENCODER_ALIGN, F_CONSOLE})


@dataclass(frozen=True)
class ControllerProfile:
    key: str
    vendor: str
    model: str
    driver: str                    # "moteus" | "st_mcsdk"
    links: tuple[str, ...]
    modes: tuple[ControlMode, ...]
    features: frozenset[str]
    min_voltage: float
    max_voltage: float
    peak_current_A: float
    default_rate_hz: float
    notes: str = ""
    url: str = ""
    # Electrical defaults used by drivers that need unit conversion.
    params: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return f"{self.vendor} {self.model}"

    def has(self, feature: str) -> bool:
        return feature in self.features

    def supports(self, mode: ControlMode) -> bool:
        return mode in self.modes


PROFILES: dict[str, ControllerProfile] = {}


def _reg(p: ControllerProfile) -> None:
    PROFILES[p.key] = p


_reg(ControllerProfile(
    key="moteus_r4_11", vendor="mjbots", model="moteus r4.11", driver="moteus",
    links=MOTEUS_LINKS, modes=MOTEUS_MODES, features=MOTEUS_FEATURES,
    min_voltage=10.0, max_voltage=44.0, peak_current_A=100.0, default_rate_hz=100.0,
    notes="CAN-FD 1/5 Mbit. Abs. max 44 V (<= 10S). Use fdcanusb or any CAN-FD adapter.",
    url="https://mjbots.com/products/moteus-r4-11"))
_reg(ControllerProfile(
    key="moteus_n1", vendor="mjbots", model="moteus n1", driver="moteus",
    links=MOTEUS_LINKS, modes=MOTEUS_MODES, features=MOTEUS_FEATURES,
    min_voltage=10.0, max_voltage=54.0, peak_current_A=100.0, default_rate_hz=100.0,
    notes="CAN-FD. Abs. max 54 V (<= 12S)."))
_reg(ControllerProfile(
    key="moteus_c1", vendor="mjbots", model="moteus c1", driver="moteus",
    links=MOTEUS_LINKS, modes=MOTEUS_MODES, features=MOTEUS_FEATURES,
    min_voltage=10.0, max_voltage=51.0, peak_current_A=20.0, default_rate_hz=100.0,
    notes="CAN-FD. Abs. max 51 V (<= 12S), 20 A peak."))
_reg(ControllerProfile(
    key="moteus_x1", vendor="mjbots", model="moteus x1", driver="moteus",
    links=MOTEUS_LINKS, modes=MOTEUS_MODES, features=MOTEUS_FEATURES,
    min_voltage=10.0, max_voltage=54.0, peak_current_A=120.0, default_rate_hz=100.0,
    notes="CAN-FD. Abs. max 54 V (<= 12S), 120 A peak."))
_reg(ControllerProfile(
    key="st_ihm08m1", vendor="STMicroelectronics",
    model="X-NUCLEO-IHM08M1 + STM32 Nucleo (MCSDK 5.x)", driver="st_mcsdk",
    links=ST_LINKS, modes=ST_MODES, features=ST_FEATURES,
    min_voltage=10.0, max_voltage=48.0, peak_current_A=15.0, default_rate_hz=20.0,
    notes=("Power stage for a Nucleo board running ST Motor Control SDK 5.x firmware "
           "with the UART 'Motor Control Protocol' enabled (ST-LINK virtual COM port). "
           "Speed and torque (Iq) control only. MCSDK has NO command watchdog: if this PC "
           "stops, the motor keeps its last setpoint - a hard-wired E-stop is mandatory."),
    url="https://www.st.com/en/ecosystems/x-nucleo-ihm08m1.html",
    params={
        # IHM08M1: 10 mOhm shunts, op-amp gain 5.18, 3.3 V ADC.  Iq digit -> A.
        "amps_per_digit": 3.3 / (65536 * 0.010 * 5.18),
        "torque_constant_Nm_per_A": 0.05,   # set to your motor's Kt
        "baudrate": 115200,
    }))
_reg(ControllerProfile(
    key="st_mcsdk_generic", vendor="STMicroelectronics",
    model="Any STM32 MCSDK 5.x board (IHM07M1, IHM16M1, EVSPIN, …)", driver="st_mcsdk",
    links=ST_LINKS, modes=ST_MODES, features=ST_FEATURES,
    min_voltage=8.0, max_voltage=48.0, peak_current_A=10.0, default_rate_hz=20.0,
    notes="Set amps_per_digit / Kt for your power board in the connection dialog.",
    params={"amps_per_digit": 3.3 / (65536 * 0.33 * 1.53), "torque_constant_Nm_per_A": 0.05,
            "baudrate": 115200}))

DEFAULT_PROFILE = "moteus_r4_11"


def get_profile(key: str | None) -> ControllerProfile:
    return PROFILES.get(key or DEFAULT_PROFILE, PROFILES[DEFAULT_PROFILE])


def make_backend(profile: ControllerProfile, config: TransportConfig):
    """Instantiate the backend for a profile + link selection."""
    if profile.driver == "moteus":
        from .moteus_backend import MoteusBackend
        return MoteusBackend(config)
    if profile.driver == "st_mcsdk":
        from .st_mcsdk_backend import StMcsdkBackend
        return StMcsdkBackend(config, profile)
    raise ValueError(f"No driver for {profile.key}")
