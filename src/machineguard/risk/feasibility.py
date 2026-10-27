"""Audit uncertain fault reports without creating supervised labels or probabilities."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from machineguard.core.provenance import sha256_file
from machineguard.features.eda import ReportWindow, load_report_windows
from machineguard.features.pipeline import verify_features
from machineguard.features.splits import SplitPlan


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True, slots=True)
class RiskFeasibilityPolicy:
    feature_version_id: str
    source_sha256: str
    horizon_seconds: tuple[int, ...]
    report_onset_known: bool
    intervention_timing_known: bool
    unreported_faults_ruled_out: bool
    independent_events_verified: bool
    time_basis: str
    use_anomaly_split_for_risk: bool
    evidence_source: str

    @classmethod
    def from_file(cls, path: Path) -> RiskFeasibilityPolicy:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("risk feasibility policy fields do not match the schema")
        horizons = data["horizon_seconds"]
        if (
            not isinstance(horizons, list)
            or not horizons
            or any(type(h) is not int or h <= 0 for h in horizons)
            or horizons != sorted(set(horizons))
        ):
            raise ValueError("horizons must be unique, positive, ascending integer seconds")
        for key in (
            "report_onset_known",
            "intervention_timing_known",
            "unreported_faults_ruled_out",
            "independent_events_verified",
            "use_anomaly_split_for_risk",
        ):
            if type(data[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        if (
            not isinstance(data["feature_version_id"], str)
            or not data["feature_version_id"]
            or not isinstance(data["source_sha256"], str)
            or len(data["source_sha256"]) != 64
            or data["time_basis"] != "source_naive_unresolved"
            or not isinstance(data["evidence_source"], str)
            or not data["evidence_source"].startswith("https://")
        ):
            raise ValueError("invalid risk evidence binding")
        data["horizon_seconds"] = tuple(horizons)
        return cls(**data)


def candidate_interval(
    window: ReportWindow, horizon_seconds: int, anchor: str
) -> tuple[datetime, datetime]:
    """Return *hypothetical* preceding exposure interval; never a positive-label interval."""
    if horizon_seconds <= 0 or anchor not in ("report_start", "report_end"):
        raise ValueError("positive horizon and supported anchor required")
    point = window.start if anchor == "report_start" else window.end_exclusive
    return point - timedelta(seconds=horizon_seconds), point


def _fold(plan: SplitPlan, start: datetime, end: datetime) -> str | None:
    """A whole interval must fit one fold; the anomaly split is diagnostic only."""
    for name in ("train", "validation", "test"):
        if getattr(plan, f"{name}_start") <= start and end <= getattr(plan, f"{name}_end"):
            return name
    return None


def audit_risk_feasibility(
    feature_version: Path, policy_path: Path, report_path: Path
) -> dict[str, Any]:
    """Count coverage around reports and issue a conservative no-model verdict.

    Counts describe observed rows near uncertain reports, not target prevalence.
    """
    policy = RiskFeasibilityPolicy.from_file(policy_path)
    manifest = verify_features(feature_version)
    if (
        manifest["version_id"] != policy.feature_version_id
        or manifest["source_sha256"] != policy.source_sha256
    ):
        raise ValueError("feature version or source hash differs from risk policy")
    if manifest["identity"]["report_windows_sha256"] != sha256_file(report_path):
        raise ValueError("report windows differ from verified feature provenance")
    plan_data = manifest["split_plan"].copy()
    for name in (
        "train_start",
        "train_end",
        "validation_start",
        "validation_end",
        "test_start",
        "test_end",
    ):
        plan_data[name] = datetime.fromisoformat(plan_data[name])
    plan = SplitPlan(**plan_data)
    windows = load_report_windows(report_path)
    report_splits = {name: 0 for name in ("train", "validation", "test", "outside_or_purge")}
    for window in windows:
        split = _fold(plan, window.start, window.end_exclusive)
        report_splits[split or "outside_or_purge"] += 1

    scenarios: list[dict[str, Any]] = []
    for horizon in policy.horizon_seconds:
        for index, window in enumerate(windows, start=1):
            for anchor in ("report_start", "report_end"):
                start, end = candidate_interval(window, horizon, anchor)
                scenarios.append(
                    {
                        "report_number": index,
                        "horizon_seconds": horizon,
                        "hypothetical_anchor": anchor,
                        "interval_start": start.isoformat(),
                        "interval_end_exclusive": end.isoformat(),
                        "whole_interval_anomaly_fold": _fold(plan, start, end),
                        "observed_rows": 0,
                    }
                )

    # Read only event time, in bounded batches. The verified manifest fixes all part hashes.
    intervals = [
        (
            datetime.fromisoformat(item["interval_start"]),
            datetime.fromisoformat(item["interval_end_exclusive"]),
        )
        for item in scenarios
    ]
    for part in manifest["parts"]:
        parquet = pq.ParquetFile(feature_version / part["path"])
        try:
            for batch in parquet.iter_batches(batch_size=20_000, columns=["event_time_source"]):
                for timestamp in batch.column(0).to_pylist():
                    for scenario, (start, end) in zip(scenarios, intervals, strict=True):
                        if start <= timestamp < end:
                            scenario["observed_rows"] += 1
        finally:
            parquet.close()

    gates = {
        "precise_onset": policy.report_onset_known,
        "intervention_chronology": policy.intervention_timing_known,
        "verified_negative_surveillance": policy.unreported_faults_ruled_out,
        "independent_events": policy.independent_events_verified,
        "risk_specific_split": policy.use_anomaly_split_for_risk,
        "training_fold_contains_report": report_splits["train"] > 0,
        "held_out_fold_contains_multiple_reports": report_splits["test"] > 1,
    }
    horizon_assessments = [
        {
            "horizon_seconds": horizon,
            "required_minimum_purge_seconds": plan.max_feature_lookback_seconds + horizon,
            "anomaly_split_purge_seconds": plan.purge_seconds,
            "anomaly_split_eligible_for_risk": False,
            "verified_positive_events": None,
            "verified_negative_rows": None,
            "status": "risk_unavailable",
        }
        for horizon in policy.horizon_seconds
    ]
    return {
        "status": "risk_unavailable",
        "model_trained": False,
        "probabilities_available": False,
        "source_sha256": policy.source_sha256,
        "feature_version_id": policy.feature_version_id,
        "policy_sha256": sha256_file(policy_path),
        "reports_sha256": sha256_file(report_path),
        "time_basis": policy.time_basis,
        "report_count": len(windows),
        "report_counts_by_anomaly_fold": report_splits,
        "independent_failure_event_count": None,
        "horizons_seconds": list(policy.horizon_seconds),
        "horizon_assessments": horizon_assessments,
        "hypothetical_exposure_coverage": scenarios,
        "gates": gates,
        "failed_gates": [name for name, passed in gates.items() if not passed],
        "target_prevalence": None,
        "calibration_metrics": None,
        "exclusion_policy": (
            "No report interval or adjacent row is assigned a supervised label; "
            "intervention and unreported-fault censoring unresolved."
        ),
        "split_caveat": (
            "Existing one-hour-purge anomaly split has zero prediction horizon "
            "and is not eligible for risk training."
        ),
        "interpretation": (
            "Hypothetical start/end intervals measure telemetry coverage only. "
            "Report timestamps are not failure onsets; rows are not positive "
            "or verified negative examples."
        ),
    }


def write_risk_audit(
    feature_version: Path, policy_path: Path, report_path: Path, output: Path
) -> Path:
    result = audit_risk_feasibility(feature_version, policy_path, report_path)
    content = _canonical(result) + b"\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.read_bytes() != content:
        raise ValueError("existing risk audit differs; choose a new output path")
    output.write_bytes(content)
    return output


def verify_risk_audit(
    path: Path, feature_version: Path, policy_path: Path, report_path: Path
) -> dict[str, Any]:
    expected = audit_risk_feasibility(feature_version, policy_path, report_path)
    if path.read_bytes() != _canonical(expected) + b"\n":
        raise ValueError("risk audit differs from verified inputs")
    return expected
