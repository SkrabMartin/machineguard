"""Versioned, explicit feature semantics for source-local event time."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    short_window_seconds: int = 300
    long_window_seconds: int = 3600
    min_short_samples: int = 3
    min_long_samples: int = 12
    max_previous_gap_seconds: int = 60
    gap_warning_seconds: int = 20
    elapsed_cap_seconds: int = 3600
    version: str = "metropt-features-1"

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("feature spec version is required")
        if (self.short_window_seconds, self.long_window_seconds) != (300, 3600):
            raise ValueError("feature names require 300-second and 3600-second windows")
        if self.min_short_samples < 2 or self.min_long_samples < 2:
            raise ValueError("minimum sample counts must be at least two")
        if (
            not 0
            < self.gap_warning_seconds
            <= self.max_previous_gap_seconds
            <= self.long_window_seconds
        ):
            raise ValueError("gap thresholds must be positive, ordered, and within lookback")
        if self.elapsed_cap_seconds > self.long_window_seconds or self.elapsed_cap_seconds <= 0:
            raise ValueError("elapsed cap must fit within maximum lookback")

    @classmethod
    def from_json_file(cls, path: Path) -> FeatureSpec:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("feature spec fields do not match the schema")
        return cls(**data)

    def to_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    inputs: tuple[str, ...]
    lookback_seconds: int
    min_samples: int
    unit: str | None
    availability: str = "at_event_time_after_current_sample"


def definitions(spec: FeatureSpec) -> tuple[FeatureDefinition, ...]:
    short = spec.short_window_seconds
    long = spec.long_window_seconds
    return (
        FeatureDefinition("tp3_mean_5m", ("tp3",), short, spec.min_short_samples, "bar"),
        FeatureDefinition("tp3_std_5m", ("tp3",), short, spec.min_short_samples, "bar"),
        FeatureDefinition(
            "motor_current_mean_5m", ("motor_current",), short, spec.min_short_samples, "A"
        ),
        FeatureDefinition(
            "oil_temperature_mean_60m", ("oil_temperature",), long, spec.min_long_samples, "degC"
        ),
        FeatureDefinition("tp3_lag1", ("tp3",), spec.max_previous_gap_seconds, 2, "bar"),
        FeatureDefinition(
            "tp3_rate_bar_s",
            ("tp3", "event_time_source"),
            spec.max_previous_gap_seconds,
            2,
            "bar/s",
        ),
        FeatureDefinition("tp3_minus_reservoirs", ("tp3", "reservoirs"), 0, 1, "bar"),
        FeatureDefinition(
            "dv_electric_transition", ("dv_electric",), spec.max_previous_gap_seconds, 2, "flag"
        ),
        FeatureDefinition(
            "loaded_run_elapsed_capped_s", ("dv_electric",), spec.elapsed_cap_seconds, 1, "s"
        ),
        FeatureDefinition("operating_load_flag", ("dv_electric",), 0, 1, "flag"),
        FeatureDefinition(
            "seconds_since_prev_capped", ("event_time_source",), spec.elapsed_cap_seconds, 2, "s"
        ),
        FeatureDefinition("duplicate_timestamp_flag", ("event_time_source",), 0, 1, "flag"),
        FeatureDefinition("sample_count_5m", ("event_time_source",), short, 1, "count"),
        FeatureDefinition("gap_count_5m", ("event_time_source",), short, 1, "count"),
    )
