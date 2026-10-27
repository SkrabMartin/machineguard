"""Synthetic numerical and temporal edge cases for Phase 4 baselines."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from machineguard.anomaly.pipeline import _eligible_for_fit
from machineguard.anomaly.statistical import (
    AlertPersistence,
    BaselinePolicy,
    RobustStat,
    Score,
    score_physical,
    score_robust,
)
from machineguard.features.eda import ReportWindow


def _policy() -> BaselinePolicy:
    return BaselinePolicy.from_json_file(Path("configs/metropt_anomaly_baseline.json"))


def _row(
    second: int = 0, state: int | None = 1, split: str = "validation", **values: float | None
) -> dict[str, object]:
    row: dict[str, object] = {
        "event_time_source": datetime(2020, 4, 1) + timedelta(seconds=second),
        "split": split,
        "operating_load_flag": state,
        "tp3_mean_5m": 8.0,
        "motor_current_mean_5m": 5.0,
        "oil_temperature_mean_60m": 60.0,
    }
    row.update(values)
    return row


def test_physical_boundaries_and_missingness() -> None:
    policy = _policy()
    assert not score_physical(_row(motor_current_mean_5m=0.1), policy).raw_alert
    assert score_physical(_row(motor_current_mean_5m=0.099), policy).triggered_signals == (
        "motor_current_mean_5m",
    )
    assert not score_physical(_row(state=0, motor_current_mean_5m=0.0), policy).raw_alert
    assert score_physical(_row(tp3_mean_5m=30.01), policy).raw_alert
    assert not score_physical(_row(tp3_mean_5m=30.0), policy).raw_alert
    assert not score_physical(_row(state=None), policy).eligible
    assert not score_physical(
        _row(tp3_mean_5m=None, oil_temperature_mean_60m=None), policy
    ).eligible


def test_robust_threshold_zero_mad_and_missing_signals() -> None:
    policy = _policy()
    stats = {
        "1": {
            "tp3_mean_5m": RobustStat(8, 1, 1000),
            "motor_current_mean_5m": RobustStat(5, 1, 1000),
            "oil_temperature_mean_60m": RobustStat(60, 0, 1000),
        }
    }
    at_threshold = 8 + 5 * 1.4826
    assert score_robust(_row(tp3_mean_5m=at_threshold), policy, stats).raw_alert
    assert not score_robust(_row(tp3_mean_5m=at_threshold - 0.0001), policy, stats).raw_alert
    assert not score_robust(_row(tp3_mean_5m=None), policy, stats).eligible
    assert not score_robust(_row(state=0), policy, stats).eligible
    assert not score_robust(_row(oil_temperature_mean_60m=1000), policy, stats).raw_alert


def test_persistence_resets_on_gap_state_split_and_unscorable() -> None:
    tracker = AlertPersistence(replace(_policy(), persistence_rows=3))
    alert = Score(True, 6.0, ("tp3_mean_5m",))
    clear = Score(True, 0.0, ())
    assert tracker.process(_row(0), alert)[:2] == (False, False)
    assert tracker.process(_row(10), alert)[:2] == (False, False)
    assert tracker.process(_row(20), alert)[:2] == (True, True)
    assert tracker.process(_row(30), alert)[:2] == (True, False)
    assert tracker.process(_row(60), alert)[:2] == (False, False)
    assert tracker.process(_row(70), alert)[:2] == (False, False)
    assert tracker.process(_row(80, state=0), alert)[:2] == (False, False)
    assert tracker.process(_row(90, state=0), alert)[:2] == (False, False)
    assert tracker.process(_row(100, state=0, split="test"), alert)[:2] == (False, False)
    assert tracker.process(_row(110, state=0, split="test"), clear)[:2] == (False, False)
    assert tracker.process(_row(120, state=0, split="test"), Score(False, None, ()))[:2] == (
        False,
        False,
    )


def test_policy_validation() -> None:
    policy = _policy()
    with pytest.raises(ValueError, match="finite"):
        replace(policy, robust_z_threshold=float("nan"))
    with pytest.raises(ValueError, match="operating states"):
        replace(policy, physical_bounds={"1": policy.physical_bounds["1"]})
    with pytest.raises(ValueError, match="sample"):
        replace(policy, persistence_rows=0)


def test_report_padding_excludes_train_fit_rows() -> None:
    policy = replace(_policy(), report_exclusion_seconds=3600)
    window = ReportWindow(datetime(2020, 4, 1, 1, 0), datetime(2020, 4, 1, 1, 1), "synthetic")
    assert not _eligible_for_fit(_row(0, split="train"), (window,), policy)
    assert _eligible_for_fit(_row(-1, split="train"), (window,), policy)
    assert not _eligible_for_fit(_row(0, split="validation"), (window,), policy)
