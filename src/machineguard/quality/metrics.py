"""Explicit, non-destructive data quality accounting for ordered telemetry."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    """Heuristic warning bounds, not certified equipment operating limits."""

    version: str = "metropt-1"
    gap_warning_seconds: int = 20
    pressure_min_bar: float = -1.0
    pressure_max_bar: float = 30.0
    temperature_min_c: float = -40.0
    temperature_max_c: float = 200.0
    current_min_a: float = 0.0
    current_max_a: float = 100.0

    def __post_init__(self) -> None:
        if not self.version or self.gap_warning_seconds <= 0:
            raise ValueError("policy version and positive gap threshold are required")
        if self.pressure_min_bar >= self.pressure_max_bar:
            raise ValueError("invalid pressure bounds")
        if self.temperature_min_c >= self.temperature_max_c:
            raise ValueError("invalid temperature bounds")
        if self.current_min_a >= self.current_max_a:
            raise ValueError("invalid current bounds")


PRESSURE_FIELDS = ("tp2", "tp3", "h1", "dv_pressure", "reservoirs")
DIGITAL_FIELDS = ("comp", "dv_electric", "towers", "mpg", "lps", "pressure_switch", "oil_level")


def _interval_bucket(seconds: int) -> str:
    if seconds in (0, 1, 9, 10, 11):
        return str(seconds)
    if seconds <= 8:
        return "2-8"
    if seconds <= 20:
        return "12-20"
    if seconds <= 60:
        return "21-60"
    if seconds <= 3600:
        return "61-3600"
    return ">3600"


@dataclass(slots=True)
class QualityAccumulator:
    policy: QualityPolicy
    rows_seen: int = 0
    rows_written: int = 0
    duplicate_timestamps: int = 0
    gap_warnings: int = 0
    max_gap_seconds: int = 0
    min_interval_seconds: int | None = None
    max_interval_seconds: int | None = None
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    interval_buckets: Counter[str] = field(default_factory=Counter)
    plausibility_counts: Counter[str] = field(default_factory=Counter)

    def observe(self, timestamp: datetime, measurements: dict[str, float]) -> None:
        self.rows_seen += 1
        if self.last_timestamp is not None:
            seconds = int((timestamp - self.last_timestamp).total_seconds())
            if seconds < 0:
                raise ValueError("timestamps must be nondecreasing")
            self.interval_buckets[_interval_bucket(seconds)] += 1
            if seconds == 0:
                self.duplicate_timestamps += 1
            if seconds > self.policy.gap_warning_seconds:
                self.gap_warnings += 1
            self.max_gap_seconds = max(self.max_gap_seconds, seconds)
            self.min_interval_seconds = (
                seconds
                if self.min_interval_seconds is None
                else min(self.min_interval_seconds, seconds)
            )
            self.max_interval_seconds = (
                seconds
                if self.max_interval_seconds is None
                else max(self.max_interval_seconds, seconds)
            )
        if self.first_timestamp is None:
            self.first_timestamp = timestamp
        self.last_timestamp = timestamp
        for name in PRESSURE_FIELDS:
            value = measurements[name]
            if not self.policy.pressure_min_bar <= value <= self.policy.pressure_max_bar:
                self.plausibility_counts[name] += 1
        value = measurements["oil_temperature"]
        if not self.policy.temperature_min_c <= value <= self.policy.temperature_max_c:
            self.plausibility_counts["oil_temperature"] += 1
        value = measurements["motor_current"]
        if not self.policy.current_min_a <= value <= self.policy.current_max_a:
            self.plausibility_counts["motor_current"] += 1
        for name in DIGITAL_FIELDS:
            if measurements[name] not in (0.0, 1.0):
                self.plausibility_counts[name] += 1
        if measurements["caudal_impulses"] < 0:
            self.plausibility_counts["caudal_impulses"] += 1
        self.rows_written += 1

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy.version,
            "rows_seen": self.rows_seen,
            "rows_written": self.rows_written,
            "rejected_rows": 0,
            "duplicate_timestamps": self.duplicate_timestamps,
            "gap_warnings": self.gap_warnings,
            "max_gap_seconds": self.max_gap_seconds,
            "min_interval_seconds": self.min_interval_seconds,
            "max_interval_seconds": self.max_interval_seconds,
            "first_timestamp_source": self.first_timestamp.isoformat()
            if self.first_timestamp
            else None,
            "last_timestamp_source": self.last_timestamp.isoformat()
            if self.last_timestamp
            else None,
            "timestamp_timezone": "source_naive_unresolved",
            "interval_buckets": dict(sorted(self.interval_buckets.items())),
            "plausibility_counts": dict(sorted(self.plausibility_counts.items())),
        }
