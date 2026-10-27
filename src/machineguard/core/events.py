"""Evidence and split contracts; interpretation of source reports is deferred."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime

from .contracts import TimeBasis


@dataclass(frozen=True, slots=True)
class Measurement:
    name: str
    value: float
    unit: str | None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("measurement name is required")
        if not math.isfinite(self.value):
            raise ValueError("measurement value must be finite")


@dataclass(frozen=True, slots=True)
class TelemetryObservation:
    """Canonical in-memory shape, without committing to a storage engine."""

    dataset_id: str
    asset_id: str
    source_sha256: str
    time_basis: TimeBasis
    event_time: datetime | None
    cycle: int | None
    measurements: tuple[Measurement, ...]

    def __post_init__(self) -> None:
        if (
            not self.dataset_id
            or not self.asset_id
            or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None
        ):
            raise ValueError("dataset, asset, and source digest are required")
        if self.time_basis is TimeBasis.WALL_CLOCK:
            if self.event_time is None or self.cycle is not None:
                raise ValueError("wall-clock observation requires event_time only")
        elif self.cycle is None or self.cycle < 1 or self.event_time is not None:
            raise ValueError("cycle observation requires a positive cycle only")
        if not self.measurements:
            raise ValueError("at least one measurement is required")
        names = [item.name for item in self.measurements]
        if len(names) != len(set(names)):
            raise ValueError("measurement names must be unique")


@dataclass(frozen=True, slots=True)
class RemainingUsefulLifeTruth:
    """Cycle-based truth for a specific engine at a specific observed cycle."""

    dataset_id: str
    asset_id: str
    observed_cycle: int
    remaining_cycles: int
    evidence_source: str

    def __post_init__(self) -> None:
        if not self.dataset_id or not self.asset_id or not self.evidence_source:
            raise ValueError("RUL identifiers and evidence_source are required")
        if self.observed_cycle < 1 or self.remaining_cycles < 0:
            raise ValueError("cycles must be positive and RUL cannot be negative")


@dataclass(frozen=True, slots=True)
class ReportedEventWindow:
    dataset_id: str
    report_id: str
    start: datetime
    end: datetime
    event_type: str
    evidence_source: str

    def __post_init__(self) -> None:
        if not all((self.dataset_id, self.report_id, self.event_type, self.evidence_source)):
            raise ValueError("event identifiers and evidence_source are required")
        if (self.start.tzinfo is None) != (self.end.tzinfo is None):
            raise ValueError("start and end must have the same timezone policy")
        if self.start >= self.end:
            raise ValueError("event window must have positive duration")


@dataclass(frozen=True, slots=True)
class SplitPolicy:
    dataset_id: str
    grouping_key: str | None
    ordering_key: str
    test_source: str
    gap_policy: str

    def __post_init__(self) -> None:
        if not all((self.dataset_id, self.ordering_key, self.test_source, self.gap_policy)):
            raise ValueError("split policy fields are required")
