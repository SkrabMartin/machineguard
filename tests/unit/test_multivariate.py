"""Synthetic model, missingness, event timing, and deterministic diagnostics."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from machineguard.anomaly.models import ModelPolicy, fit_state
from machineguard.anomaly.multivariate import _DiagnosticSample, _vector
from machineguard.anomaly.statistical import BaselinePolicy
from machineguard.evaluation.events import EventEvaluation
from machineguard.features.eda import ReportWindow


def _policy() -> ModelPolicy:
    return ModelPolicy.from_json_file(Path("configs/metropt_isolation_forest.json"))


def _baseline_policy() -> BaselinePolicy:
    return BaselinePolicy.from_json_file(Path("configs/metropt_anomaly_baseline.json"))


def _row(second: int, split: str = "validation", state: int = 0) -> dict[str, object]:
    return {
        "asset_id": "synthetic-asset",
        "source_row_index": second,
        "event_time_source": datetime(2020, 4, 1) + timedelta(seconds=second),
        "split": split,
        "operating_load_flag": state,
        "tp3_mean_5m": 1.0,
        "tp3_std_5m": 0.2,
        "tp3_minus_reservoirs": 0.01,
        "motor_current_mean_5m": 2.0,
        "oil_temperature_mean_60m": 60.0,
        "loaded_run_elapsed_capped_s": 10,
    }


def test_policy_and_complete_case_contract() -> None:
    policy = _policy()
    assert len(policy.signals) == 6
    assert _vector(_row(0)) is not None
    missing = _row(0)
    missing["oil_temperature_mean_60m"] = None
    assert _vector(missing) is None
    with pytest.raises(ValueError, match="contamination"):
        replace(policy, contamination=0.0)
    with pytest.raises(ValueError, match="training"):
        replace(policy, max_samples=20, min_group_samples=10)


def test_train_only_scaler_and_seed_are_deterministic() -> None:
    policy = replace(_policy(), n_estimators=12, max_samples=4, min_group_samples=4)
    matrix = np.asarray(
        [[float(i), 0.2 + i * 0.01, 0.01, 2.0 + i, 60.0 + i, 0.0] for i in range(12)],
        dtype=np.float64,
    )
    first = fit_state(matrix, policy)
    second = fit_state(matrix, policy)
    assert first.metadata() == second.metadata()
    assert np.array_equal(first.decision_function(matrix), second.decision_function(matrix))
    assert first.scale[-1] == 1.0  # constant feature uses safe unit scale
    assert np.isfinite(first.diagnostic_deltas(matrix[-1])).all()
    with pytest.raises(ValueError, match="nonfinite"):
        fit_state(np.full((12, 6), np.nan), policy)


def test_event_endpoint_sensitivity_and_persistence() -> None:
    baseline = replace(_baseline_policy(), persistence_rows=3, report_exclusion_seconds=0)
    window = ReportWindow(datetime(2020, 4, 1), datetime(2020, 4, 1, 0, 1), "synthetic")
    evaluator = EventEvaluation(baseline, (window,))
    for second in (0, 10, 20, 30):
        evaluator.observe(_row(second), 0.1 if second == 0 else -0.1)
    event = evaluator.result()["reported_windows"][0]
    assert event["active_rows_inclusive_minute"] == 1
    assert event["active_rows_exact_endpoint"] == 0
    assert event["any_active_inclusive_minute"] is True
    assert event["any_active_exact_endpoint"] is False
    assert event["first_active_relative_to_report_start_seconds"] == 30


def test_diagnostic_sample_is_order_independent_and_bounded() -> None:
    rows = [_row(index) for index in range(50)]
    first = _DiagnosticSample(5)
    second = _DiagnosticSample(5)
    for row in rows:
        first.consider(row, [1.0] * 6)
    for row in reversed(rows):
        second.consider(row, [1.0] * 6)
    assert {entry[1] for entry in first.samples["validation"]} == {
        entry[1] for entry in second.samples["validation"]
    }
    assert len(first.samples["validation"]) == 5


def test_unseen_state_is_unscorable() -> None:
    from machineguard.anomaly.multivariate import _score_experiment

    policy = _policy()
    baseline = _baseline_policy()
    row = _row(0, state=2)
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as patch:
        patch.setattr(
            "machineguard.anomaly.multivariate._iter_batches",
            lambda *_args: iter([[row]]),
        )
        result = _score_experiment(Path("unused"), {}, {}, policy, baseline, (), 1)
    assert result["metrics"]["validation"]["unscorable_rows"] == 1
    assert result["metrics"]["validation"]["episodes"] == 0


def test_event_metrics_match_phase4_protocol_for_identical_raw_alerts() -> None:
    from machineguard.anomaly.pipeline import _Evaluation
    from machineguard.anomaly.statistical import Score

    baseline = replace(_baseline_policy(), persistence_rows=2, report_exclusion_seconds=0)
    window = ReportWindow(datetime(2020, 4, 2), datetime(2020, 4, 2, 0, 1), "synthetic")
    phase4 = _Evaluation(baseline, (window,))
    phase5 = EventEvaluation(baseline, (window,))
    for second, alert in ((0, False), (10, True), (20, True), (30, True), (50, False)):
        row = _row(second)
        baseline_score = Score(True, 1.0 if alert else 0.0, ("tp3_mean_5m",) if alert else ())
        phase4.observe(row, {"physical": baseline_score, "robust_mad": baseline_score})
        phase5.observe(row, -0.1 if alert else 0.1)
    earlier = phase4.result()["metrics"]["physical"]["validation"]
    later = phase5.result()["metrics"]["validation"]
    for name in (
        "rows",
        "eligible_rows",
        "raw_alert_rows",
        "active_alert_rows",
        "episodes",
        "off_report_episode_starts",
        "observed_off_report_seconds",
        "off_report_episodes_per_observed_day",
    ):
        assert earlier[name] == later[name]
