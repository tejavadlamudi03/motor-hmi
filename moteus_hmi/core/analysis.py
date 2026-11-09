"""Step-response metrics used by the tuning tab."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class StepMetrics:
    amplitude: float
    rise_time_s: float          # 10% -> 90%
    overshoot_pct: float
    settling_time_s: float      # within +-2% band and stays there
    steady_state_error: float
    peak_torque: float

    def as_rows(self) -> list[tuple[str, str]]:
        def f(v, unit, p=4):
            return "n/a" if not math.isfinite(v) else f"{v:.{p}f} {unit}".strip()
        return [
            ("Step amplitude", f(self.amplitude, "rev")),
            ("Rise time (10–90 %)", f(self.rise_time_s * 1000, "ms", 1)),
            ("Overshoot", f(self.overshoot_pct, "%", 1)),
            ("Settling time (±2 %)", f(self.settling_time_s * 1000, "ms", 1)),
            ("Steady-state error", f(self.steady_state_error, "rev", 5)),
            ("Peak torque", f(self.peak_torque, "N·m", 3)),
        ]


def step_metrics(t: list[float], y: list[float], y0: float, target: float,
                 torque: list[float] | None = None, band: float = 0.02) -> StepMetrics:
    amp = target - y0
    nan = math.nan
    pts = [(ti, yi) for ti, yi in zip(t, y) if math.isfinite(yi)]
    if abs(amp) < 1e-9 or len(pts) < 5:
        return StepMetrics(amp, nan, nan, nan, nan, nan)
    # Normalised response 0 -> 1.
    norm = [(ti, (yi - y0) / amp) for ti, yi in pts]

    def first_cross(level: float) -> float:
        for ti, ni in norm:
            if ni >= level:
                return ti
        return nan

    t10, t90 = first_cross(0.1), first_cross(0.9)
    rise = t90 - t10 if math.isfinite(t10) and math.isfinite(t90) else nan
    peak = max(ni for _, ni in norm)
    overshoot = max(0.0, (peak - 1.0) * 100.0)
    settle = nan
    for i in range(len(norm)):
        if all(abs(ni - 1.0) <= band for _, ni in norm[i:]):
            settle = norm[i][0]
            break
    tail = [yi for _, yi in pts[-max(3, len(pts) // 10):]]
    sse = target - sum(tail) / len(tail)
    pk_t = max((abs(v) for v in (torque or []) if math.isfinite(v)), default=nan)
    return StepMetrics(amp, rise, overshoot, settle, sse, pk_t)
