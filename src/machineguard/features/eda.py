"""Streaming descriptive audit; source-reported windows are context, not labels."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ReportWindow:
    start: datetime
    end_exclusive: datetime
    description: str


def load_report_windows(path: Path) -> tuple[ReportWindow, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        data.get("evidence_type") != "reported_fault_window"
        or data.get("not_row_label") is not True
    ):
        raise ValueError("event windows must be explicitly marked as reports, not row labels")
    windows = []
    for item in data["windows"]:
        start = datetime.fromisoformat(item["start"])
        end = datetime.fromisoformat(item["end_inclusive_minute"]) + timedelta(minutes=1)
        if start.tzinfo is not None or end.tzinfo is not None or start >= end:
            raise ValueError("invalid source-naive report window")
        windows.append(ReportWindow(start, end, item["description"]))
    if not windows or any(
        a.end_exclusive > b.start for a, b in zip(windows, windows[1:], strict=False)
    ):
        raise ValueError("report windows must be ordered and disjoint")
    return tuple(windows)


class RunningStats:
    def __init__(self) -> None:
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.minimum = math.inf
        self.maximum = -math.inf

    def observe(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "mean": self.mean if self.count else None,
            "std_population": math.sqrt(self.m2 / self.count) if self.count else None,
            "min": self.minimum if self.count else None,
            "max": self.maximum if self.count else None,
        }


class EDAAccumulator:
    """Constant-memory sensor and operating-state summary."""

    SENSORS = (
        "tp3",
        "reservoirs",
        "motor_current",
        "oil_temperature",
        "pressure_switch",
        "dv_electric",
        "comp",
        "towers",
    )

    def __init__(self, windows: tuple[ReportWindow, ...]) -> None:
        self.windows = windows
        self.sensors = {name: RunningStats() for name in self.SENSORS}
        self.current_by_load = {"0": RunningStats(), "1": RunningStats(), "other": RunningStats()}
        self.state_counts: dict[str, dict[str, int]] = {
            name: {} for name in ("dv_electric", "comp", "towers")
        }
        self.report_counts = [0] * len(windows)
        self.preceding_hour_counts = [0] * len(windows)
        self.rows = 0

    def observe(self, row: dict[str, Any]) -> None:
        self.rows += 1
        for name, stats in self.sensors.items():
            stats.observe(float(row[name]))
        for name, counts in self.state_counts.items():
            key = str(row[name])
            counts[key] = counts.get(key, 0) + 1
        load = str(int(row["dv_electric"])) if row["dv_electric"] in (0, 1) else "other"
        self.current_by_load[load].observe(float(row["motor_current"]))
        timestamp = row["event_time_source"]
        for index, window in enumerate(self.windows):
            if window.start <= timestamp < window.end_exclusive:
                self.report_counts[index] += 1
            if window.start - timedelta(hours=1) <= timestamp < window.start:
                self.preceding_hour_counts[index] += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "sensors": {name: stats.to_dict() for name, stats in self.sensors.items()},
            "states": self.state_counts,
            "motor_current_by_dv_electric": {
                name: stats.to_dict() for name, stats in self.current_by_load.items()
            },
            "reported_fault_windows": [
                {
                    "start": window.start.isoformat(),
                    "end_exclusive": window.end_exclusive.isoformat(),
                    "description": window.description,
                    "rows_in_window": self.report_counts[index],
                    "rows_preceding_hour": self.preceding_hour_counts[index],
                }
                for index, window in enumerate(self.windows)
            ],
            "interpretation": (
                "Source-reported intervals are contextual evidence, not precise "
                "row-level failure labels."
            ),
        }
