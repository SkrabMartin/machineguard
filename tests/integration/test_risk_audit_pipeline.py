"""Synthetic verified-feature integration tests for the no-model gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_anomaly_pipeline import _feature_fixture

from machineguard.core.provenance import sha256_file
from machineguard.features.pipeline import verify_features
from machineguard.risk.feasibility import (
    audit_risk_feasibility,
    verify_risk_audit,
    write_risk_audit,
)


def _policy(feature_version: Path, path: Path) -> Path:
    data = json.loads(Path("configs/metropt_risk_feasibility.json").read_text())
    data["feature_version_id"] = feature_version.name
    data["source_sha256"] = verify_features(feature_version)["source_sha256"]
    data["horizon_seconds"] = [10, 3600]
    path.write_text(json.dumps(data))
    return path


def test_no_labels_or_probabilities_and_reproducible_audit(tmp_path: Path) -> None:
    feature_version, _, windows = _feature_fixture(tmp_path)
    policy = _policy(feature_version, tmp_path / "risk-policy.json")
    output = tmp_path / "risk.json"
    result = audit_risk_feasibility(feature_version, policy, windows)
    assert result["status"] == "risk_unavailable"
    assert result["model_trained"] is False
    assert result["probabilities_available"] is False
    assert result["target_prevalence"] is None
    assert result["horizon_assessments"] == [
        {
            "horizon_seconds": 10,
            "required_minimum_purge_seconds": 3610,
            "anomaly_split_purge_seconds": 3600,
            "anomaly_split_eligible_for_risk": False,
            "verified_positive_events": None,
            "verified_negative_rows": None,
            "status": "risk_unavailable",
        },
        {
            "horizon_seconds": 3600,
            "required_minimum_purge_seconds": 7200,
            "anomaly_split_purge_seconds": 3600,
            "anomaly_split_eligible_for_risk": False,
            "verified_positive_events": None,
            "verified_negative_rows": None,
            "status": "risk_unavailable",
        },
    ]
    assert result["report_counts_by_anomaly_fold"] == {
        "train": 0,
        "validation": 1,
        "test": 0,
        "outside_or_purge": 0,
    }
    cases = result["hypothetical_exposure_coverage"]
    assert (
        next(
            c
            for c in cases
            if c["horizon_seconds"] == 10 and c["hypothetical_anchor"] == "report_start"
        )["observed_rows"]
        == 0
    )
    assert (
        next(
            c
            for c in cases
            if c["horizon_seconds"] == 3600 and c["hypothetical_anchor"] == "report_start"
        )["observed_rows"]
        == 0
    )
    assert (
        next(
            c
            for c in cases
            if c["horizon_seconds"] == 3600 and c["hypothetical_anchor"] == "report_end"
        )["observed_rows"]
        == 3
    )
    assert "training_fold_contains_report" in result["failed_gates"]
    assert (
        next(
            c
            for c in cases
            if c["horizon_seconds"] == 3600 and c["hypothetical_anchor"] == "report_start"
        )["whole_interval_anomaly_fold"]
        is None
    )
    write_risk_audit(feature_version, policy, windows, output)
    first_hash = sha256_file(output)
    write_risk_audit(feature_version, policy, windows, output)
    assert sha256_file(output) == first_hash
    assert verify_risk_audit(output, feature_version, policy, windows) == result
    output.write_text("tampered")
    with pytest.raises(ValueError, match="differs"):
        verify_risk_audit(output, feature_version, policy, windows)


def test_changed_report_or_feature_binding_rejected(tmp_path: Path) -> None:
    feature_version, _, windows = _feature_fixture(tmp_path)
    policy = _policy(feature_version, tmp_path / "risk-policy.json")
    data = json.loads(windows.read_text())
    data["windows"][0]["start"] = "2020-02-01T01:01:00"
    windows.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="report windows differ"):
        audit_risk_feasibility(feature_version, policy, windows)
    data = json.loads(policy.read_text())
    data["feature_version_id"] = "wrong"
    policy.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="feature version"):
        audit_risk_feasibility(feature_version, policy, windows)
