"""Explicitly synthetic replay semantics and checkpoint state tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from machineguard.anomaly.statistical import BaselinePolicy, RobustStat
from machineguard.features.engine import FeatureEngine, FeatureInput
from machineguard.features.spec import FeatureSpec
from machineguard.features.splits import SplitPlan
from machineguard.replay.engine import ReplayEngine, ReplayPolicy

START = datetime(2020, 6, 15, 1)


def _row(index: int, second: int, pressure: float = 2.0) -> FeatureInput:
    return FeatureInput(
        "synthetic-asset",
        index,
        START + timedelta(seconds=second),
        pressure,
        1.0,
        pressure * 2,
        20.0 + pressure,
        1.0,
    )


def _scorer() -> tuple[BaselinePolicy, dict[str, dict[str, RobustStat]]]:
    policy = BaselinePolicy.from_json_file(Path("configs/metropt_anomaly_baseline.json"))
    stats = {
        state: {signal: RobustStat(2.0, 1.0, 1000) for signal in policy.signals}
        for state in ("0", "1")
    }
    return policy, stats


def test_reorder_ties_speed_and_feature_parity() -> None:
    split = SplitPlan.from_json_file(Path("configs/metropt_anomaly_split.json"))
    spec = FeatureSpec(min_short_samples=2, min_long_samples=2)
    policy, stats = _scorer()
    sleeps: list[float] = []
    replay = ReplayEngine(spec, split, ReplayPolicy(10, 10), policy, stats, sleeps.append)
    arrivals = [_row(0, 0), _row(1, 20, 4), _row(2, 10, 3), _row(3, 20, 5)]
    decisions = []
    for row in arrivals:
        ready, notices = replay.ingest(row)
        assert not notices
        decisions.extend(ready)
    decisions.extend(replay.finish())
    assert [item["event_id"] for item in decisions] == [
        "synthetic-asset:0",
        "synthetic-asset:2",
        "synthetic-asset:1",
        "synthetic-asset:3",
    ]
    assert sleeps == [1.0, 1.0]
    reference = FeatureEngine(spec)
    for decision, row in zip(
        decisions,
        sorted(arrivals, key=lambda item: (item.event_time_source, item.source_row_index)),
        strict=True,
    ):
        output = reference.process(row)
        assert decision["features"]["tp3_mean_5m"] == output.tp3_mean_5m
        assert decision["features"]["duplicate_timestamp_flag"] == output.duplicate_timestamp_flag
        assert set(decision["scores"]) == {"physical", "robust_mad"}


def test_duplicates_late_gaps_and_snapshot_resume() -> None:
    split = SplitPlan.from_json_file(Path("configs/metropt_anomaly_split.json"))
    spec = FeatureSpec(min_short_samples=2, min_long_samples=2)
    policy, stats = _scorer()
    config = ReplayPolicy(10, 0)
    first = ReplayEngine(spec, split, config, policy, stats)
    initial = []
    for row in (_row(0, 0), _row(1, 20, 4)):
        ready, _ = first.ingest(row)
        initial.extend(ready)
    snapshot = first.snapshot()
    restored = ReplayEngine.from_snapshot(spec, split, config, snapshot, policy, stats)
    remaining = [_row(2, 10, 3), _row(1, 20, 4), _row(3, 5), _row(4, 100)]
    decisions = list(initial)
    qualities = []
    for row in remaining:
        ready, notice = restored.ingest(row)
        decisions.extend(ready)
        qualities.extend(notice)
    decisions.extend(restored.finish())
    assert [item["kind"] for item in qualities] == ["duplicate_key", "late_event_quarantined"]
    assert restored.duplicates == 1 and restored.late == 1
    assert restored.gaps == 1
    assert decisions[-1]["quality"]["gap_seconds"] == 80.0
    assert restored.decisions == 4
    with pytest.raises(ValueError, match="specification mismatch"):
        FeatureEngine.from_snapshot(
            FeatureSpec(min_short_samples=3), snapshot["engines"]["synthetic-asset"]
        )


def test_invalid_replay_policy() -> None:
    with pytest.raises(ValueError, match="lateness"):
        ReplayPolicy(max_lateness_seconds=-1)
    with pytest.raises(ValueError, match="speed"):
        ReplayPolicy(speed=float("nan"))
