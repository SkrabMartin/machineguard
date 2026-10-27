"""Synthetic risk-evidence boundary tests; these are not failure labels."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from machineguard.features.eda import ReportWindow
from machineguard.risk.feasibility import RiskFeasibilityPolicy, candidate_interval


def test_candidate_interval_has_exclusive_hypothetical_anchor() -> None:
    window = ReportWindow(datetime(2020, 1, 2), datetime(2020, 1, 3), "synthetic")
    assert candidate_interval(window, 3600, "report_start") == (
        datetime(2020, 1, 1, 23),
        datetime(2020, 1, 2),
    )
    assert candidate_interval(window, 3600, "report_end") == (
        datetime(2020, 1, 2, 23),
        datetime(2020, 1, 3),
    )
    with pytest.raises(ValueError):
        candidate_interval(window, 0, "report_start")
    with pytest.raises(ValueError):
        candidate_interval(window, 3600, "actual_failure")


@pytest.mark.parametrize("horizons", [[0], [3600, 3600], [21600, 3600], [1.5], []])
def test_invalid_horizon_policy_rejected(tmp_path: Path, horizons: list[object]) -> None:
    policy = json.loads(Path("configs/metropt_risk_feasibility.json").read_text())
    policy["horizon_seconds"] = horizons
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="horizons"):
        RiskFeasibilityPolicy.from_file(path)


def test_policy_rejects_untyped_evidence_and_fields(tmp_path: Path) -> None:
    policy = json.loads(Path("configs/metropt_risk_feasibility.json").read_text())
    policy["report_onset_known"] = "false"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="boolean"):
        RiskFeasibilityPolicy.from_file(path)
    policy["report_onset_known"] = False
    policy["unexpected"] = 1
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="schema"):
        RiskFeasibilityPolicy.from_file(path)
