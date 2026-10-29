"""Event-time reorder, quality policy, causal features, and baseline decisions."""

from __future__ import annotations

import heapq
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from machineguard.anomaly.statistical import (
    AlertPersistence,
    BaselinePolicy,
    RobustStat,
    Score,
    score_physical,
    score_robust,
)
from machineguard.features.engine import FeatureEngine, FeatureInput
from machineguard.features.spec import FeatureSpec
from machineguard.features.splits import SplitPlan


@dataclass(frozen=True, slots=True)
class ReplayPolicy:
    max_lateness_seconds: int = 0
    speed: float = 0.0
    checkpoint_every: int = 1000

    def __post_init__(self) -> None:
        if self.max_lateness_seconds < 0 or self.checkpoint_every < 1:
            raise ValueError("lateness must be nonnegative and checkpoint interval positive")
        if not math.isfinite(self.speed) or self.speed < 0:
            raise ValueError("speed must be finite and nonnegative; zero means unthrottled")


def _event_data(row: FeatureInput) -> dict[str, Any]:
    return {**asdict(row), "event_time_source": row.event_time_source.isoformat()}


def _event_from_data(data: dict[str, Any]) -> FeatureInput:
    return FeatureInput(
        **{**data, "event_time_source": datetime.fromisoformat(data["event_time_source"])}
    )


class ReplayEngine:
    """One ordered arrival stream; unique source row indexes increase in arrival order."""

    def __init__(
        self,
        spec: FeatureSpec,
        split: SplitPlan,
        policy: ReplayPolicy,
        baseline_policy: BaselinePolicy | None = None,
        robust_stats: dict[str, dict[str, RobustStat]] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if (baseline_policy is None) != (robust_stats is None):
            raise ValueError("baseline policy and robust statistics must be provided together")
        self.spec = spec
        self.split = split
        self.policy = policy
        self.baseline_policy = baseline_policy
        self.robust_stats = robust_stats
        self.sleeper = sleeper
        self.engines: dict[str, FeatureEngine] = {}
        self.alerts: dict[str, dict[str, AlertPersistence]] = {}
        self.pending: list[tuple[datetime, int, FeatureInput]] = []
        self.max_event_time: datetime | None = None
        self.last_emitted_time: datetime | None = None
        self.max_source_row_index = -1
        self.arrivals = 0
        self.decisions = 0
        self.duplicates = 0
        self.late = 0
        self.gaps = 0
        self.processing_ns = 0
        self.max_processing_ns = 0
        self.latency_buckets = [0, 0, 0, 0, 0]

    def ingest(self, row: FeatureInput) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return newly ready decisions and quality notices."""
        self.arrivals += 1
        if row.event_time_source.tzinfo is not None or row.source_row_index < 0:
            raise ValueError("replay requires source-naive time and nonnegative source row index")
        if row.source_row_index <= self.max_source_row_index:
            self.duplicates += 1
            return [], [{"kind": "duplicate_key", "event": _event_data(row)}]
        self.max_source_row_index = row.source_row_index
        watermark = (
            self.max_event_time - timedelta(seconds=self.policy.max_lateness_seconds)
            if self.max_event_time is not None
            else None
        )
        if (watermark is not None and row.event_time_source < watermark) or (
            self.last_emitted_time is not None and row.event_time_source < self.last_emitted_time
        ):
            self.late += 1
            return [], [{"kind": "late_event_quarantined", "event": _event_data(row)}]
        if self.max_event_time is None or row.event_time_source > self.max_event_time:
            self.max_event_time = row.event_time_source
        heapq.heappush(self.pending, (row.event_time_source, row.source_row_index, row))
        ready_until = self.max_event_time - timedelta(seconds=self.policy.max_lateness_seconds)
        return self._drain(ready_until), []

    def finish(self) -> list[dict[str, Any]]:
        return self._drain(None)

    def _drain(self, ready_until: datetime | None) -> list[dict[str, Any]]:
        decisions = []
        while self.pending and (ready_until is None or self.pending[0][0] <= ready_until):
            timestamp, _, row = heapq.heappop(self.pending)
            if self.last_emitted_time is not None and timestamp < self.last_emitted_time:
                raise ValueError("event-time order violated during replay")
            if self.policy.speed > 0 and self.last_emitted_time is not None:
                delay = (timestamp - self.last_emitted_time).total_seconds() / self.policy.speed
                if delay > 0:
                    self.sleeper(delay)
            started = time.perf_counter_ns()
            decision = self._decide(row)
            elapsed = time.perf_counter_ns() - started
            self.processing_ns += elapsed
            self.max_processing_ns = max(self.max_processing_ns, elapsed)
            bucket = next(
                (
                    i
                    for i, limit in enumerate((1_000_000, 10_000_000, 100_000_000, 1_000_000_000))
                    if elapsed < limit
                ),
                4,
            )
            self.latency_buckets[bucket] += 1
            decisions.append(decision)
            self.last_emitted_time = timestamp
            self.decisions += 1
        return decisions

    def _decide(self, row: FeatureInput) -> dict[str, Any]:
        engine = self.engines.get(row.asset_id)
        if engine is None:
            engine = FeatureEngine(self.spec)
            self.engines[row.asset_id] = engine
        prior = engine.previous_event_time
        gap_seconds = (row.event_time_source - prior).total_seconds() if prior is not None else None
        features = engine.process(row)
        split = self.split.assign(row.event_time_source)
        feature_row = asdict(features)
        feature_data = {**feature_row, "event_time_source": row.event_time_source.isoformat()}
        if gap_seconds is not None and gap_seconds > self.spec.gap_warning_seconds:
            self.gaps += 1
        scores: dict[str, Any] = {}
        if self.baseline_policy is not None and self.robust_stats is not None:
            alert_states = self.alerts.setdefault(
                row.asset_id,
                {
                    name: AlertPersistence(self.baseline_policy)
                    for name in ("physical", "robust_mad")
                },
            )
            scoring_row = {**feature_row, "split": split}
            for name in ("physical", "robust_mad"):
                if split not in ("validation", "test"):
                    score = Score(False, None, ())
                elif name == "physical":
                    score = score_physical(scoring_row, self.baseline_policy)
                else:
                    score = score_robust(scoring_row, self.baseline_policy, self.robust_stats)
                active, episode, start = alert_states[name].process(scoring_row, score)
                scores[name] = {
                    "eligible": score.eligible,
                    "value": score.value,
                    "triggered_signals": score.triggered_signals,
                    "active": active,
                    "new_episode": episode,
                    "episode_start": start.isoformat() if start else None,
                }
        return {
            "event_id": f"{row.asset_id}:{row.source_row_index}",
            "event_time_source": row.event_time_source.isoformat(),
            "split": split,
            "features": feature_data,
            "quality": {
                "gap_seconds": gap_seconds
                if gap_seconds and gap_seconds > self.spec.gap_warning_seconds
                else None,
                "duplicate_timestamp": bool(features.duplicate_timestamp_flag),
            },
            "scores": scores,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "engines": {name: engine.snapshot() for name, engine in self.engines.items()},
            "alerts": {
                asset: {name: alert.snapshot() for name, alert in items.items()}
                for asset, items in self.alerts.items()
            },
            "pending": [_event_data(item[2]) for item in sorted(self.pending)],
            "max_event_time": self.max_event_time.isoformat() if self.max_event_time else None,
            "last_emitted_time": self.last_emitted_time.isoformat()
            if self.last_emitted_time
            else None,
            "max_source_row_index": self.max_source_row_index,
            "arrivals": self.arrivals,
            "decisions": self.decisions,
            "duplicates": self.duplicates,
            "late": self.late,
            "gaps": self.gaps,
            "processing_ns": self.processing_ns,
            "max_processing_ns": self.max_processing_ns,
            "latency_buckets": self.latency_buckets,
        }

    @classmethod
    def from_snapshot(
        cls,
        spec: FeatureSpec,
        split: SplitPlan,
        policy: ReplayPolicy,
        state: dict[str, Any],
        baseline_policy: BaselinePolicy | None = None,
        robust_stats: dict[str, dict[str, RobustStat]] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> ReplayEngine:
        engine = cls(spec, split, policy, baseline_policy, robust_stats, sleeper)
        engine.engines = {
            name: FeatureEngine.from_snapshot(spec, item) for name, item in state["engines"].items()
        }
        if baseline_policy is not None:
            engine.alerts = {
                asset: {
                    name: AlertPersistence.from_snapshot(baseline_policy, item)
                    for name, item in items.items()
                }
                for asset, items in state["alerts"].items()
            }
        for item in state["pending"]:
            row = _event_from_data(item)
            heapq.heappush(engine.pending, (row.event_time_source, row.source_row_index, row))
        engine.max_event_time = (
            datetime.fromisoformat(state["max_event_time"]) if state["max_event_time"] else None
        )
        engine.last_emitted_time = (
            datetime.fromisoformat(state["last_emitted_time"])
            if state["last_emitted_time"]
            else None
        )
        for name in (
            "max_source_row_index",
            "arrivals",
            "decisions",
            "duplicates",
            "late",
            "gaps",
            "processing_ns",
            "max_processing_ns",
        ):
            setattr(engine, name, int(state[name]))
        engine.latency_buckets = list(state["latency_buckets"])
        return engine
