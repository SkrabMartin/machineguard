"""Synthetic hand-calculated causal features and split boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from machineguard.features.eda import load_report_windows
from machineguard.features.engine import FeatureEngine, FeatureInput
from machineguard.features.spec import FeatureSpec, definitions
from machineguard.features.splits import SplitPlan


def _row(second: int, pressure: float, load: float = 1.0) -> FeatureInput:
    return FeatureInput(
        "asset-1",
        second,
        datetime(2020, 1, 1) + timedelta(seconds=second),
        pressure,
        1.0,
        pressure * 2,
        20.0 + pressure,
        load,
    )


def test_trailing_statistics_and_availability() -> None:
    engine = FeatureEngine(FeatureSpec(min_long_samples=3))
    a = engine.process(_row(0, 2))
    b = engine.process(_row(10, 4))
    c = engine.process(_row(20, 6))
    assert a.tp3_mean_5m is None and b.tp3_mean_5m is None
    assert c.tp3_mean_5m == pytest.approx(4)
    assert c.tp3_std_5m == pytest.approx((8 / 3) ** 0.5)
    assert c.motor_current_mean_5m == pytest.approx(8)
    assert c.oil_temperature_mean_60m == pytest.approx(24)
    assert c.tp3_lag1 == 4 and c.tp3_rate_bar_s == pytest.approx(0.2)
    assert c.tp3_minus_reservoirs == 5
    assert c.sample_count_5m == 3 and c.gap_count_5m == 0
    assert c.loaded_run_elapsed_capped_s == 20
    assert c.operating_load_flag == 1


def test_window_expiry_gap_transition_and_duplicates() -> None:
    engine = FeatureEngine(FeatureSpec())
    engine.process(_row(0, 2))
    after_gap = engine.process(_row(300, 4, 0))
    assert after_gap.sample_count_5m == 1 and after_gap.gap_count_5m == 1
    assert after_gap.tp3_lag1 is None and after_gap.tp3_rate_bar_s is None
    assert after_gap.dv_electric_transition is None
    assert after_gap.loaded_run_elapsed_capped_s == 0
    duplicate = engine.process(_row(300, 6, 1))
    assert duplicate.duplicate_timestamp_flag == 1
    assert duplicate.tp3_rate_bar_s is None
    assert duplicate.dv_electric_transition is None
    transition = engine.process(_row(310, 8, 0))
    assert transition.dv_electric_transition == 1
    assert transition.operating_load_flag == 0
    much_later = engine.process(_row(4000, 9, 0))
    assert much_later.gap_count_5m == 0
    assert much_later.seconds_since_prev_capped is None
    with pytest.raises(ValueError, match="nondecreasing"):
        engine.process(_row(309, 1))


def test_future_mutation_cannot_change_prior_rows() -> None:
    prefix = [_row(i * 10, float(i)) for i in range(20)]
    future = [_row(200, 40), _row(210, 42)]
    changed = [_row(200, -900), _row(210, -901)]
    first = [FeatureEngine(FeatureSpec()).process(prefix[0])]
    # Compare full runs, including state across all preceding rows.
    outputs_a = [
        engine.process(row) for engine in [FeatureEngine(FeatureSpec())] for row in prefix + future
    ]
    outputs_b = [
        engine.process(row) for engine in [FeatureEngine(FeatureSpec())] for row in prefix + changed
    ]
    assert outputs_a[: len(prefix)] == outputs_b[: len(prefix)]
    assert first[0] == outputs_a[0]


def test_split_purge_and_source_binding() -> None:
    path = Path("configs/metropt_anomaly_split.json")
    split = SplitPlan.from_json_file(path)
    assert split.assign(split.train_end - timedelta(seconds=1)) == "train"
    assert split.assign(split.train_end) == "purge"
    assert split.assign(split.validation_start) == "validation"
    assert split.assign(split.validation_end) == "purge"
    assert split.assign(split.test_start) == "test"
    assert split.assign(split.test_end) == "outside"
    assert split.to_dict()["train_start"] == split.train_start.isoformat()
    with pytest.raises(ValueError, match="purge"):
        replace(split, purge_seconds=3599)
    with pytest.raises(ValueError, match="not risk"):
        replace(split, prediction_horizon_seconds=3600)


def test_spec_registry_and_report_windows() -> None:
    spec = FeatureSpec.from_json_file(Path("configs/metropt_feature_spec.json"))
    assert len(definitions(spec)) == 14
    assert max(item.lookback_seconds for item in definitions(spec)) == 3600
    with pytest.raises(ValueError, match="feature names"):
        FeatureSpec(short_window_seconds=60)
    with pytest.raises(ValueError, match="within lookback"):
        FeatureSpec(max_previous_gap_seconds=4000)
    windows = load_report_windows(Path("configs/metropt_report_windows.json"))
    assert len(windows) == 4
    assert all(a.end_exclusive <= b.start for a, b in zip(windows, windows[1:], strict=False))


def test_purge_prevents_prior_split_from_affecting_first_validation_row() -> None:
    spec = FeatureSpec()
    first = [_row(0, 2, 1), _row(10, 4, 1), _row(3611, 6, 1)]
    altered_train = [_row(0, 200, 0), _row(10, 400, 0), _row(3611, 6, 1)]
    engine_a = FeatureEngine(spec)
    engine_b = FeatureEngine(spec)
    result_a = [engine_a.process(row) for row in first]
    result_b = [engine_b.process(row) for row in altered_train]
    assert result_a[-1] == result_b[-1]
    assert result_a[-1].seconds_since_prev_capped is None
    assert result_a[-1].gap_count_5m == 0
