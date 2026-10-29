"""Pure, stateful, trailing-window feature calculations."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from .spec import FeatureSpec


@dataclass(frozen=True, slots=True)
class FeatureInput:
    asset_id: str
    source_row_index: int
    event_time_source: datetime
    tp3: float
    reservoirs: float
    motor_current: float
    oil_temperature: float
    dv_electric: float


@dataclass(frozen=True, slots=True)
class FeatureOutput:
    asset_id: str
    source_row_index: int
    event_time_source: datetime
    tp3_mean_5m: float | None
    tp3_std_5m: float | None
    motor_current_mean_5m: float | None
    oil_temperature_mean_60m: float | None
    tp3_lag1: float | None
    tp3_rate_bar_s: float | None
    tp3_minus_reservoirs: float
    dv_electric_transition: int | None
    loaded_run_elapsed_capped_s: int | None
    operating_load_flag: int | None
    seconds_since_prev_capped: int | None
    duplicate_timestamp_flag: int
    sample_count_5m: int
    gap_count_5m: int


class FeatureEngine:
    """One asset, event-time ordered; all windows include current row and past only."""

    def __init__(self, spec: FeatureSpec) -> None:
        self.spec = spec
        self._short: deque[tuple[datetime, float, float]] = deque()
        self._long: deque[tuple[datetime, float]] = deque()
        self._gaps: deque[datetime] = deque()
        self._tp3_sum = 0.0
        self._tp3_sumsq = 0.0
        self._current_sum = 0.0
        self._temperature_sum = 0.0
        self._previous: FeatureInput | None = None
        self._load_start: datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        """Export bounded rolling state without executable serialization."""
        return {
            "spec": self.spec.to_dict(),
            "short": [
                (time.isoformat(), pressure, current) for time, pressure, current in self._short
            ],
            "long": [(time.isoformat(), value) for time, value in self._long],
            "gaps": [time.isoformat() for time in self._gaps],
            "tp3_sum": self._tp3_sum,
            "tp3_sumsq": self._tp3_sumsq,
            "current_sum": self._current_sum,
            "temperature_sum": self._temperature_sum,
            "previous": (
                {
                    **asdict(self._previous),
                    "event_time_source": self._previous.event_time_source.isoformat(),
                }
                if self._previous is not None
                else None
            ),
            "load_start": self._load_start.isoformat() if self._load_start else None,
        }

    @property
    def previous_event_time(self) -> datetime | None:
        return self._previous.event_time_source if self._previous is not None else None

    @classmethod
    def from_snapshot(cls, spec: FeatureSpec, state: dict[str, Any]) -> FeatureEngine:
        """Restore an integrity-checked checkpoint for the same feature specification."""
        if state["spec"] != spec.to_dict():
            raise ValueError("feature checkpoint specification mismatch")
        engine = cls(spec)
        engine._short = deque(
            (datetime.fromisoformat(time), float(pressure), float(current))
            for time, pressure, current in state["short"]
        )
        engine._long = deque(
            (datetime.fromisoformat(time), float(value)) for time, value in state["long"]
        )
        engine._gaps = deque(datetime.fromisoformat(time) for time in state["gaps"])
        engine._tp3_sum = float(state["tp3_sum"])
        engine._tp3_sumsq = float(state["tp3_sumsq"])
        engine._current_sum = float(state["current_sum"])
        engine._temperature_sum = float(state["temperature_sum"])
        previous = state["previous"]
        if previous is not None:
            engine._previous = FeatureInput(
                **{
                    **previous,
                    "event_time_source": datetime.fromisoformat(previous["event_time_source"]),
                }
            )
        engine._load_start = (
            datetime.fromisoformat(state["load_start"]) if state["load_start"] else None
        )
        return engine

    def process(self, row: FeatureInput) -> FeatureOutput:
        if row.event_time_source.tzinfo is not None:
            raise ValueError("feature input must retain source-naive time")
        if self._previous is not None and row.asset_id != self._previous.asset_id:
            raise ValueError("FeatureEngine requires one asset; create one engine per asset")
        prior = self._previous
        delta: int | None = None
        if prior is not None:
            seconds = (row.event_time_source - prior.event_time_source).total_seconds()
            if seconds < 0 or int(seconds) != seconds:
                raise ValueError("feature events require nondecreasing whole-second time")
            delta = int(seconds)
            if self.spec.gap_warning_seconds < delta <= self.spec.short_window_seconds:
                self._gaps.append(row.event_time_source)
        short_start = row.event_time_source - timedelta(seconds=self.spec.short_window_seconds)
        long_start = row.event_time_source - timedelta(seconds=self.spec.long_window_seconds)
        while self._short and self._short[0][0] <= short_start:
            _, pressure, current = self._short.popleft()
            self._tp3_sum -= pressure
            self._tp3_sumsq -= pressure * pressure
            self._current_sum -= current
        while self._long and self._long[0][0] <= long_start:
            _, temperature = self._long.popleft()
            self._temperature_sum -= temperature
        while self._gaps and self._gaps[0] <= short_start:
            self._gaps.popleft()
        self._short.append((row.event_time_source, row.tp3, row.motor_current))
        self._long.append((row.event_time_source, row.oil_temperature))
        self._tp3_sum += row.tp3
        self._tp3_sumsq += row.tp3 * row.tp3
        self._current_sum += row.motor_current
        self._temperature_sum += row.oil_temperature
        short_count = len(self._short)
        long_count = len(self._long)
        short_ready = short_count >= self.spec.min_short_samples
        long_ready = long_count >= self.spec.min_long_samples
        pressure_mean = self._tp3_sum / short_count if short_ready else None
        pressure_std = None
        if pressure_mean is not None:
            variance = self._tp3_sumsq / short_count - pressure_mean * pressure_mean
            pressure_std = math.sqrt(max(0.0, variance))
        valid_previous = delta is not None and 0 < delta <= self.spec.max_previous_gap_seconds
        lag = prior.tp3 if prior is not None and valid_previous else None
        rate = (row.tp3 - lag) / delta if lag is not None and delta is not None else None
        load = int(row.dv_electric) if row.dv_electric in (0.0, 1.0) else None
        prior_load = (
            int(prior.dv_electric)
            if prior is not None and prior.dv_electric in (0.0, 1.0)
            else None
        )
        transition = (
            int(load != prior_load)
            if valid_previous and load is not None and prior_load is not None
            else None
        )
        if load != 1:
            self._load_start = None
            loaded_elapsed = 0 if load == 0 else None
        else:
            if self._load_start is None or not valid_previous or prior_load != 1:
                self._load_start = row.event_time_source
            loaded_elapsed = min(
                self.spec.elapsed_cap_seconds,
                int((row.event_time_source - self._load_start).total_seconds()),
            )
        output = FeatureOutput(
            asset_id=row.asset_id,
            source_row_index=row.source_row_index,
            event_time_source=row.event_time_source,
            tp3_mean_5m=pressure_mean,
            tp3_std_5m=pressure_std,
            motor_current_mean_5m=self._current_sum / short_count if short_ready else None,
            oil_temperature_mean_60m=(self._temperature_sum / long_count if long_ready else None),
            tp3_lag1=lag,
            tp3_rate_bar_s=rate,
            tp3_minus_reservoirs=row.tp3 - row.reservoirs,
            dv_electric_transition=transition,
            loaded_run_elapsed_capped_s=loaded_elapsed,
            operating_load_flag=load,
            seconds_since_prev_capped=(
                min(delta, self.spec.elapsed_cap_seconds)
                if delta is not None and delta <= self.spec.elapsed_cap_seconds
                else None
            ),
            duplicate_timestamp_flag=int(delta == 0),
            sample_count_5m=short_count,
            gap_count_5m=len(self._gaps),
        )
        self._previous = row
        return output
