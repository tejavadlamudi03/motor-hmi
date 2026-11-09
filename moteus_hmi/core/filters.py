"""Streaming (sample-by-sample) noise reduction filters for feedback signals.

These run inside the HMI on telemetry received over CAN-FD (typically
50-500 Hz).  They clean up *what the operator sees, what gets logged
and what the HMI safety supervisor evaluates*.  They do NOT sit inside
the moteus closed loop - that loop runs at 15-60 kHz on the controller
and is filtered by the controller's own encoder PLL
(``motor_position.sources.N.pll_filter_hz``) and current-loop
bandwidth (``servo.pid_dq_hz``), both editable from the Calibration tab.

Every filter:
  * passes NaN through without corrupting its state,
  * initialises to the first valid sample (no start-up transient),
  * is pure Python so it has no heavy dependencies and is unit tested.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Iterable


class Filter(ABC):
    kind = "none"

    @abstractmethod
    def update(self, x: float, dt: float) -> float:
        ...

    def reset(self) -> None:
        pass


class PassThrough(Filter):
    kind = "none"

    def update(self, x: float, dt: float) -> float:
        return x


class EmaFilter(Filter):
    """First-order low-pass (exponential moving average).

    The smoothing factor is recomputed from the real sample interval so
    the cutoff stays correct even with loop jitter.
    """

    kind = "ema"

    def __init__(self, cutoff_hz: float):
        if cutoff_hz <= 0:
            raise ValueError("cutoff_hz must be > 0")
        self.cutoff_hz = float(cutoff_hz)
        self._y: float | None = None

    def reset(self) -> None:
        self._y = None

    def update(self, x: float, dt: float) -> float:
        if not math.isfinite(x):
            return x
        if self._y is None or dt <= 0:
            self._y = x
            return x
        alpha = 1.0 - math.exp(-2.0 * math.pi * self.cutoff_hz * dt)
        self._y += alpha * (x - self._y)
        return self._y


class Biquad(Filter):
    """Direct-form-II-transposed biquad with steady-state initialisation."""

    kind = "biquad"

    def __init__(self, b0: float, b1: float, b2: float, a1: float, a2: float):
        self.b0, self.b1, self.b2, self.a1, self.a2 = b0, b1, b2, a1, a2
        self._z1: float | None = None
        self._z2 = 0.0

    def reset(self) -> None:
        self._z1 = None
        self._z2 = 0.0

    def _init_state(self, x0: float) -> None:
        # Steady state for a constant input x0 (assumes unity DC gain for
        # low-pass / notch designs).
        dc = (self.b0 + self.b1 + self.b2) / (1.0 + self.a1 + self.a2)
        y0 = dc * x0
        self._z2 = self.b2 * x0 - self.a2 * y0
        self._z1 = self.b1 * x0 - self.a1 * y0 + self._z2

    def update(self, x: float, dt: float) -> float:
        if not math.isfinite(x):
            return x
        if self._z1 is None:
            self._init_state(x)
        y = self.b0 * x + self._z1
        self._z1 = self.b1 * x - self.a1 * y + self._z2
        self._z2 = self.b2 * x - self.a2 * y
        return y


class ButterworthLowPass(Biquad):
    """2nd order Butterworth low-pass (bilinear transform, pre-warped)."""

    kind = "butterworth"

    def __init__(self, cutoff_hz: float, sample_rate_hz: float):
        nyquist = sample_rate_hz / 2.0
        if not (0 < cutoff_hz < nyquist):
            raise ValueError(
                f"cutoff {cutoff_hz} Hz must be between 0 and Nyquist ({nyquist:.1f} Hz)")
        self.cutoff_hz = float(cutoff_hz)
        self.sample_rate_hz = float(sample_rate_hz)
        k = math.tan(math.pi * cutoff_hz / sample_rate_hz)
        q = 1.0 / math.sqrt(2.0)
        norm = 1.0 / (1.0 + k / q + k * k)
        b0 = k * k * norm
        super().__init__(b0, 2.0 * b0, b0,
                         2.0 * (k * k - 1.0) * norm,
                         (1.0 - k / q + k * k) * norm)


class NotchFilter(Biquad):
    """Band-stop biquad, e.g. to remove mains pickup (50/60 Hz) or a
    mechanical resonance / cogging frequency."""

    kind = "notch"

    def __init__(self, center_hz: float, q: float, sample_rate_hz: float):
        nyquist = sample_rate_hz / 2.0
        if not (0 < center_hz < nyquist):
            raise ValueError(
                f"notch {center_hz} Hz must be between 0 and Nyquist ({nyquist:.1f} Hz)")
        if q <= 0:
            raise ValueError("q must be > 0")
        self.center_hz = float(center_hz)
        self.q = float(q)
        w0 = 2.0 * math.pi * center_hz / sample_rate_hz
        alpha = math.sin(w0) / (2.0 * q)
        cosw = math.cos(w0)
        a0 = 1.0 + alpha
        super().__init__(1.0 / a0, -2.0 * cosw / a0, 1.0 / a0,
                         -2.0 * cosw / a0, (1.0 - alpha) / a0)


class MedianFilter(Filter):
    """Sliding median - removes isolated spikes (e.g. a corrupted frame)
    while preserving edges better than a linear low-pass."""

    kind = "median"

    def __init__(self, window: int = 5):
        if window < 3 or window % 2 == 0:
            raise ValueError("median window must be odd and >= 3")
        self.window = int(window)
        self._buf: deque[float] = deque(maxlen=self.window)

    def reset(self) -> None:
        self._buf.clear()

    def update(self, x: float, dt: float) -> float:
        if not math.isfinite(x):
            return x
        self._buf.append(x)
        s = sorted(self._buf)
        return s[len(s) // 2]


class HampelFilter(Filter):
    """Outlier rejection: a sample further than ``n_sigma`` robust
    standard deviations (1.4826 * MAD) from the window median is replaced
    by the median; normal samples pass through untouched (no added lag)."""

    kind = "hampel"

    def __init__(self, window: int = 7, n_sigma: float = 3.0):
        if window < 3:
            raise ValueError("hampel window must be >= 3")
        if n_sigma <= 0:
            raise ValueError("n_sigma must be > 0")
        self.window = int(window)
        self.n_sigma = float(n_sigma)
        self._buf: deque[float] = deque(maxlen=self.window)
        self.rejected = 0

    def reset(self) -> None:
        self._buf.clear()
        self.rejected = 0

    def update(self, x: float, dt: float) -> float:
        if not math.isfinite(x):
            return x
        if len(self._buf) < 3:
            self._buf.append(x)
            return x
        s = sorted(self._buf)
        med = s[len(s) // 2]
        mad = sorted(abs(v - med) for v in s)[len(s) // 2]
        sigma = 1.4826 * mad
        if sigma > 0 and abs(x - med) > self.n_sigma * sigma:
            self.rejected += 1
            # Do not let the outlier into the window.
            self._buf.append(med)
            return med
        self._buf.append(x)
        return x


class Kalman1D(Filter):
    """Scalar Kalman filter with a random-walk process model.

    ``process_noise`` (q) is the expected variance growth of the true
    signal per second; ``measurement_noise`` (r) is the sensor variance.
    Larger r/q -> smoother but slower.
    """

    kind = "kalman"

    def __init__(self, process_noise: float, measurement_noise: float):
        if process_noise <= 0 or measurement_noise <= 0:
            raise ValueError("process and measurement noise must be > 0")
        self.q = float(process_noise)
        self.r = float(measurement_noise)
        self._x: float | None = None
        self._p = 0.0

    def reset(self) -> None:
        self._x = None
        self._p = 0.0

    def update(self, x: float, dt: float) -> float:
        if not math.isfinite(x):
            return x
        if self._x is None:
            self._x, self._p = x, self.r
            return x
        self._p += self.q * max(dt, 1e-6)
        k = self._p / (self._p + self.r)
        self._x += k * (x - self._x)
        self._p *= (1.0 - k)
        return self._x


class FilterChain(Filter):
    kind = "chain"

    def __init__(self, stages: Iterable[Filter]):
        self.stages = [s for s in stages if not isinstance(s, PassThrough)]

    def reset(self) -> None:
        for s in self.stages:
            s.reset()

    def update(self, x: float, dt: float) -> float:
        for s in self.stages:
            x = s.update(x, dt)
        return x


FILTER_TYPES = {
    "none": "None",
    "ema": "EMA low-pass (1st order)",
    "butterworth": "Butterworth low-pass (2nd order)",
    "kalman": "Kalman (random walk)",
    "median": "Median (spike removal)",
    "hampel": "Hampel (outlier rejection)",
    "notch": "Notch (band-stop)",
}


def build_filter(spec: dict[str, Any] | None, sample_rate_hz: float) -> Filter:
    """Build one stage from a JSON-able spec, e.g.
    ``{"type": "butterworth", "cutoff_hz": 10}``."""
    if not spec:
        return PassThrough()
    kind = spec.get("type", "none")
    if kind == "none":
        return PassThrough()
    if kind == "ema":
        return EmaFilter(float(spec.get("cutoff_hz", 10.0)))
    if kind == "butterworth":
        return ButterworthLowPass(float(spec.get("cutoff_hz", 10.0)), sample_rate_hz)
    if kind == "notch":
        return NotchFilter(float(spec.get("center_hz", 50.0)),
                           float(spec.get("q", 5.0)), sample_rate_hz)
    if kind == "median":
        return MedianFilter(int(spec.get("window", 5)))
    if kind == "hampel":
        return HampelFilter(int(spec.get("window", 7)), float(spec.get("n_sigma", 3.0)))
    if kind == "kalman":
        return Kalman1D(float(spec.get("process_noise", 1.0)),
                        float(spec.get("measurement_noise", 0.01)))
    raise ValueError(f"unknown filter type {kind!r}")


def build_chain(specs: Iterable[dict[str, Any]] | None, sample_rate_hz: float) -> FilterChain:
    return FilterChain(build_filter(s, sample_rate_hz) for s in (specs or []))


def adapt_chain(specs: Iterable[dict[str, Any]] | None, sample_rate_hz: float) -> list[dict[str, Any]]:
    """Make a (default / stored) chain valid for the actual loop rate: low-pass
    cut-offs are limited to 40 % of the sample rate and notches above
    Nyquist are dropped.  Used for defaults; operator input is validated
    strictly instead."""
    out = []
    lim = 0.4 * sample_rate_hz
    for s in specs or []:
        s = dict(s)
        if s.get("type") in ("ema", "butterworth") and float(s.get("cutoff_hz", 0)) >= lim:
            s["cutoff_hz"] = round(lim, 3)
        if s.get("type") == "notch" and float(s.get("center_hz", 0)) >= 0.5 * sample_rate_hz:
            continue
        out.append(s)
    return out


def validate_chain(specs: Iterable[dict[str, Any]] | None, sample_rate_hz: float) -> str | None:
    """Return an error message, or None when the chain is valid."""
    try:
        build_chain(specs, sample_rate_hz)
    except (ValueError, TypeError) as e:
        return str(e)
    return None


# Sensible defaults: spike rejection + mild smoothing on the noisy
# derived quantities, nothing on position (already clean).
DEFAULT_FILTERS: dict[str, list[dict[str, Any]]] = {
    "position": [],
    "velocity": [{"type": "hampel", "window": 7, "n_sigma": 3.0},
                 {"type": "butterworth", "cutoff_hz": 15.0}],
    "torque": [{"type": "hampel", "window": 7, "n_sigma": 3.0},
               {"type": "butterworth", "cutoff_hz": 15.0}],
    "q_current": [{"type": "hampel", "window": 7, "n_sigma": 3.0},
                  {"type": "butterworth", "cutoff_hz": 15.0}],
    "d_current": [{"type": "butterworth", "cutoff_hz": 15.0}],
    "power": [{"type": "ema", "cutoff_hz": 5.0}],
    "voltage": [{"type": "median", "window": 5}, {"type": "ema", "cutoff_hz": 2.0}],
    "temperature": [{"type": "ema", "cutoff_hz": 0.5}],
    "motor_temperature": [{"type": "ema", "cutoff_hz": 0.5}],
    "position_error": [{"type": "butterworth", "cutoff_hz": 15.0}],
    "velocity_error": [{"type": "butterworth", "cutoff_hz": 15.0}],
}


class NoiseMeter:
    """Rolling mean / standard deviation / peak-to-peak over N samples."""

    def __init__(self, window: int = 200):
        self._buf: deque[float] = deque(maxlen=window)

    def add(self, x: float) -> None:
        if math.isfinite(x):
            self._buf.append(x)

    def reset(self) -> None:
        self._buf.clear()

    @property
    def count(self) -> int:
        return len(self._buf)

    def stats(self) -> tuple[float, float, float]:
        n = len(self._buf)
        if n < 2:
            return (math.nan, math.nan, math.nan)
        mean = sum(self._buf) / n
        var = sum((v - mean) ** 2 for v in self._buf) / (n - 1)
        return (mean, math.sqrt(var), max(self._buf) - min(self._buf))


def detrended_std(values: list[float]) -> float:
    """Std-dev of first differences / sqrt(2): a noise estimate that is
    insensitive to slow motion (white-noise assumption)."""
    v = [x for x in values if math.isfinite(x)]
    if len(v) < 3:
        return math.nan
    d = [b - a for a, b in zip(v, v[1:])]
    m = sum(d) / len(d)
    var = sum((x - m) ** 2 for x in d) / (len(d) - 1)
    return math.sqrt(var / 2.0)
