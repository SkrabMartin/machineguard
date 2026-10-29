"""State-aware physical and robust-MAD baselines with causal alert persistence."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SIGNALS = ("tp3_mean_5m", "motor_current_mean_5m", "oil_temperature_mean_60m")
ROBUST_SCALE = 1.4826


@dataclass(frozen=True, slots=True)
class BaselinePolicy:
    version: str
    feature_version_id: str
    signals: tuple[str, ...]
    robust_z_threshold: float
    min_group_samples: int
    min_scoring_signals: int
    persistence_rows: int
    max_contiguous_seconds: int
    report_exclusion_seconds: int
    physical_bounds: dict[str, dict[str, tuple[float, float]]]
    physical_rationale: str

    @classmethod
    def from_json_file(cls, path: Path) -> BaselinePolicy:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("baseline policy fields do not match schema")
        data["signals"] = tuple(data["signals"])
        data["physical_bounds"] = {
            state: {name: tuple(bounds) for name, bounds in rules.items()}
            for state, rules in data["physical_bounds"].items()
        }
        return cls(**data)

    def __post_init__(self) -> None:
        if not self.version or not self.feature_version_id or not self.physical_rationale:
            raise ValueError("baseline policy identity and rationale are required")
        if self.signals != SIGNALS:
            raise ValueError("baseline signals must match the approved Phase 4 feature set")
        if not math.isfinite(self.robust_z_threshold) or self.robust_z_threshold <= 0:
            raise ValueError("robust z threshold must be finite and positive")
        if (
            self.min_group_samples < 2
            or not 1 <= self.min_scoring_signals <= len(SIGNALS)
            or self.persistence_rows < 1
            or self.max_contiguous_seconds < 1
            or self.report_exclusion_seconds < 0
        ):
            raise ValueError("invalid baseline sample, persistence, or exclusion policy")
        if set(self.physical_bounds) != {"0", "1"}:
            raise ValueError("physical bounds require operating states 0 and 1")
        for rules in self.physical_bounds.values():
            if set(rules) != set(SIGNALS):
                raise ValueError("physical bounds require every approved signal")
            for lower, upper in rules.values():
                if not all(math.isfinite(value) for value in (lower, upper)) or lower >= upper:
                    raise ValueError("physical bounds must be finite and increasing")


@dataclass(frozen=True, slots=True)
class RobustStat:
    median: float
    mad: float
    count: int

    @property
    def supported(self) -> bool:
        return self.count >= 2 and self.mad > 0 and math.isfinite(self.mad)


@dataclass(frozen=True, slots=True)
class Score:
    eligible: bool
    value: float | None
    triggered_signals: tuple[str, ...]

    @property
    def raw_alert(self) -> bool:
        return self.eligible and bool(self.triggered_signals)


def _state(row: dict[str, Any]) -> str | None:
    value = row.get("operating_load_flag")
    return str(value) if value in (0, 1) else None


def score_physical(row: dict[str, Any], policy: BaselinePolicy) -> Score:
    state = _state(row)
    if state is None:
        return Score(False, None, ())
    triggered = []
    observed = 0
    for name in policy.signals:
        value = row.get(name)
        if value is None or not math.isfinite(value):
            continue
        observed += 1
        lower, upper = policy.physical_bounds[state][name]
        if value < lower or value > upper:
            triggered.append(name)
    if observed < policy.min_scoring_signals:
        return Score(False, None, ())
    return Score(True, float(len(triggered)), tuple(triggered))


def score_robust(
    row: dict[str, Any], policy: BaselinePolicy, stats: dict[str, dict[str, RobustStat]]
) -> Score:
    state = _state(row)
    if state is None or state not in stats:
        return Score(False, None, ())
    triggered = []
    scores = []
    for name in policy.signals:
        value = row.get(name)
        fitted = stats[state].get(name)
        if value is None or not math.isfinite(value) or fitted is None or not fitted.supported:
            continue
        z = abs(value - fitted.median) / (ROBUST_SCALE * fitted.mad)
        scores.append(z)
        if z >= policy.robust_z_threshold:
            triggered.append(name)
    if len(scores) < policy.min_scoring_signals:
        return Score(False, None, ())
    return Score(True, max(scores), tuple(triggered))


class AlertPersistence:
    """Activate after N consecutive alerting eligible rows in one state/split."""

    def __init__(self, policy: BaselinePolicy) -> None:
        self.policy = policy
        self._prior_time: datetime | None = None
        self._prior_state: str | None = None
        self._prior_split: str | None = None
        self._run_count = 0
        self._run_start: datetime | None = None
        self._active = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "prior_time": self._prior_time.isoformat() if self._prior_time else None,
            "prior_state": self._prior_state,
            "prior_split": self._prior_split,
            "run_count": self._run_count,
            "run_start": self._run_start.isoformat() if self._run_start else None,
            "active": self._active,
        }

    @classmethod
    def from_snapshot(cls, policy: BaselinePolicy, state: dict[str, Any]) -> AlertPersistence:
        result = cls(policy)
        result._prior_time = (
            datetime.fromisoformat(state["prior_time"]) if state["prior_time"] else None
        )
        result._prior_state = state["prior_state"]
        result._prior_split = state["prior_split"]
        result._run_count = int(state["run_count"])
        result._run_start = (
            datetime.fromisoformat(state["run_start"]) if state["run_start"] else None
        )
        result._active = bool(state["active"])
        return result

    def process(self, row: dict[str, Any], score: Score) -> tuple[bool, bool, datetime | None]:
        timestamp: datetime = row["event_time_source"]
        state = _state(row)
        split: str = row["split"]
        contiguous = (
            self._prior_time is not None
            and self._prior_state == state
            and self._prior_split == split
            and 0
            < (timestamp - self._prior_time).total_seconds()
            <= self.policy.max_contiguous_seconds
        )
        if not contiguous or not score.raw_alert:
            self._run_count = 0
            self._run_start = None
            self._active = False
        new_episode = False
        if score.raw_alert:
            if self._run_count == 0:
                self._run_start = timestamp
            self._run_count += 1
            if self._run_count >= self.policy.persistence_rows:
                new_episode = not self._active
                self._active = True
        self._prior_time = timestamp
        self._prior_state = state
        self._prior_split = split
        return self._active, new_episode, self._run_start if self._active else None
