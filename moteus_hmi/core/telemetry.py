"""Per-motor feedback processing: filtering + noise statistics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .filters import DEFAULT_FILTERS, FilterChain, NoiseMeter, adapt_chain, build_chain, detrended_std
from .types import SIGNALS, Telemetry


@dataclass
class Sample:
    raw: Telemetry
    filtered: dict[str, float]


@dataclass
class NoiseReport:
    signal: str
    raw_std: float
    filtered_std: float

    @property
    def reduction_db(self) -> float:
        if (math.isfinite(self.raw_std) and math.isfinite(self.filtered_std)
                and self.raw_std > 0 and self.filtered_std > 0):
            return 20.0 * math.log10(self.raw_std / self.filtered_std)
        return math.nan


class TelemetryProcessor:
    """Applies the configured filter chain to every continuous signal and
    keeps short windows for noise estimation."""

    NOISE_WINDOW = 200

    def __init__(self, sample_rate_hz: float, specs: Optional[dict[str, list[dict[str, Any]]]] = None):
        self.sample_rate_hz = float(sample_rate_hz)
        self.specs: dict[str, list[dict[str, Any]]] = {
            k: list(v) for k, v in DEFAULT_FILTERS.items()}
        if specs:
            self.specs.update({k: list(v) for k, v in specs.items() if k in SIGNALS})
        self.chains: dict[str, FilterChain] = {}
        self._raw_win: dict[str, list[float]] = {s: [] for s in SIGNALS}
        self._flt_win: dict[str, list[float]] = {s: [] for s in SIGNALS}
        self._last_t: Optional[float] = None
        self._rebuild()

    def _rebuild(self) -> None:
        self.specs = {k: adapt_chain(v, self.sample_rate_hz) for k, v in self.specs.items()}
        self.chains = {s: build_chain(self.specs.get(s, []), self.sample_rate_hz) for s in SIGNALS}

    def set_filter(self, signal: str, specs: list[dict[str, Any]]) -> None:
        if signal not in SIGNALS:
            raise ValueError(f"unknown signal {signal}")
        chain = build_chain(specs, self.sample_rate_hz)   # validates
        self.specs[signal] = list(specs)
        self.chains[signal] = chain
        self._flt_win[signal].clear()

    def set_sample_rate(self, rate_hz: float) -> None:
        self.sample_rate_hz = float(rate_hz)
        self._rebuild()

    def process(self, t: Telemetry) -> Sample:
        dt = (t.timestamp - self._last_t) if self._last_t is not None else 1.0 / self.sample_rate_hz
        self._last_t = t.timestamp
        out: dict[str, float] = {}
        for s, chain in self.chains.items():
            x = t.value(s)
            y = chain.update(x, dt)
            out[s] = y
            rw, fw = self._raw_win[s], self._flt_win[s]
            rw.append(x)
            fw.append(y)
            if len(rw) > self.NOISE_WINDOW:
                del rw[0]
            if len(fw) > self.NOISE_WINDOW:
                del fw[0]
        return Sample(raw=t, filtered=out)

    def noise(self) -> list[NoiseReport]:
        return [NoiseReport(s, detrended_std(self._raw_win[s]), detrended_std(self._flt_win[s]))
                for s in SIGNALS]
