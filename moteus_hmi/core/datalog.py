"""CSV data recorder (raw + filtered feedback) with SHA-256 sidecar."""

from __future__ import annotations

import csv
import hashlib
import io
import math
import time
from pathlib import Path
from typing import Optional

from .telemetry import Sample
from .types import SIGNALS

RAW_FIELDS = ["mode", "fault", "home_state", "trajectory_complete",
              "control_position", "control_velocity"] + list(SIGNALS)


class DataRecorder:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path: Optional[Path] = None
        self._fh: Optional[io.TextIOWrapper] = None
        self._writer = None
        self._t0 = 0.0
        self.rows = 0
        self._last_flush = 0.0

    @property
    def active(self) -> bool:
        return self._fh is not None

    def start(self, user: str, note: str = "") -> Path:
        if self.active:
            self.stop()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = self.directory / f"recording-{stamp}.csv"
        self._fh = open(self.path, "w", newline="", encoding="utf-8")
        self._fh.write(f"# Motor HMI recording, started {time.strftime('%Y-%m-%d %H:%M:%S %z')} by {user}\n")
        if note:
            self._fh.write(f"# note: {note}\n")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(["t_s", "motor_id"] + RAW_FIELDS + [f"{s}_filt" for s in SIGNALS])
        self._t0 = time.monotonic()
        self.rows = 0
        return self.path

    def write(self, sample: Sample) -> None:
        if not self._writer:
            return
        r = sample.raw
        raw = r.to_dict()

        def fmt(v):
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, float):
                return "" if math.isnan(v) else f"{v:.6g}"
            return v

        self._writer.writerow([f"{r.timestamp - self._t0:.4f}", r.motor_id]
                              + [fmt(raw[k]) for k in RAW_FIELDS]
                              + [fmt(sample.filtered.get(s, math.nan)) for s in SIGNALS])
        self.rows += 1
        now = time.monotonic()
        if now - self._last_flush > 1.0:
            self._fh.flush()
            self._last_flush = now

    def stop(self) -> Optional[Path]:
        if not self._fh:
            return None
        self._fh.close()
        self._fh = None
        self._writer = None
        path = self.path
        if path:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            Path(str(path) + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
        return path
