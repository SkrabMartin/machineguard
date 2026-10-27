"""Synthetic end-to-end statistical baseline reproducibility and train isolation."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from machineguard import __version__
from machineguard.anomaly.pipeline import run_baselines, verify_baselines
from machineguard.core.catalog import METROPT3
from machineguard.core.provenance import SourceManifest
from machineguard.features.pipeline import build_features
from machineguard.ingestion.pipeline import ingest_metropt


def _feature_fixture(tmp_path: Path, validation_pressure: float = 20.0) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "synthetic.csv"
    header = [field.raw_name for field in METROPT3.fields]
    rows = []
    schedule = [
        *((i, f"2020-02-01 00:00:{i * 10:02d}", float(i + 2), i >= 4) for i in range(6)),
        (6, "2020-02-01 00:01:00", 8.0, True),
        (7, "2020-02-01 00:01:10", 9.0, True),
        (8, "2020-02-01 01:02:00", validation_pressure, True),
        (9, "2020-02-01 01:02:10", validation_pressure + 1, True),
        (10, "2020-02-01 01:02:20", validation_pressure + 2, True),
        (11, "2020-02-01 02:04:00", 10.0, False),
        (12, "2020-02-01 02:04:10", 11.0, False),
        (13, "2020-02-01 02:04:20", 12.0, False),
    ]
    for index, timestamp, pressure, load in schedule:
        values = {name: "1" for name in header[2:]}
        values["TP3"] = str(pressure)
        values["Motor_current"] = str(pressure)
        values["DV_eletric"] = "1" if load else "0"
        rows.append([str(index), timestamp, *(values[name] for name in header[2:])])
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)
    provenance = SourceManifest.from_file(
        source,
        dataset_id="metropt-3-uci",
        source_url="https://example.org/synthetic",
        retrieved_at_utc=datetime(2026, 10, 27, tzinfo=UTC),
        license_status="synthetic test fixture",
        schema_version="1",
        parser_version=__version__,
    )
    canonical = ingest_metropt(source, tmp_path / "canonical", provenance, batch_size=3)
    split = {
        "dataset_version_id": canonical.name,
        "source_sha256": provenance.sha256,
        "train_start": "2020-02-01T00:00:00",
        "train_end": "2020-02-01T00:01:20",
        "validation_start": "2020-02-01T01:01:20",
        "validation_end": "2020-02-01T01:03:00",
        "test_start": "2020-02-01T02:03:00",
        "test_end": "2020-02-01T02:05:00",
        "purge_seconds": 3600,
        "max_feature_lookback_seconds": 3600,
        "prediction_horizon_seconds": 0,
        "usage": "anomaly_descriptive_only",
        "timezone_policy": "source_naive_unresolved",
    }
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps(split))
    windows = {
        "evidence_type": "reported_fault_window",
        "not_row_label": True,
        "windows": [
            {
                "start": "2020-02-01T01:02:00",
                "end_inclusive_minute": "2020-02-01T01:02:00",
                "description": "synthetic reported interval",
            }
        ],
    }
    windows_path = tmp_path / "windows.json"
    windows_path.write_text(json.dumps(windows))
    feature_version = build_features(
        canonical,
        tmp_path / "features",
        Path("configs/metropt_feature_spec.json"),
        split_path,
        windows_path,
        batch_size=3,
    )
    policy = json.loads(Path("configs/metropt_anomaly_baseline.json").read_text())
    policy["feature_version_id"] = feature_version.name
    policy["min_group_samples"] = 2
    policy["persistence_rows"] = 2
    policy["report_exclusion_seconds"] = 0
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    return feature_version, policy_path, windows_path


def test_fit_uses_train_only_and_output_is_verified(tmp_path: Path) -> None:
    feature_version, policy, windows = _feature_fixture(tmp_path)
    output = run_baselines(feature_version, tmp_path / "out", policy, windows, batch_size=3)
    manifest = verify_baselines(output)
    artifact = json.loads((output / "baseline.json").read_text())
    evaluation = json.loads((output / "evaluation.json").read_text())
    assert manifest["version_id"] == output.name
    assert artifact["fit_summary"]["fit_rows_by_state"] == {"0": 4, "1": 4}
    assert evaluation["metrics"]["robust_mad"]["validation"]["rows"] == 3
    assert set(evaluation["metrics"]["robust_mad"]["validation"]["triggered_signal_rows"]) == set(
        artifact["policy"]["signals"]
    )
    assert evaluation["metrics"]["robust_mad"]["test"]["rows"] == 3
    assert evaluation["reported_windows"]["robust_mad"][0]["split"] == "validation"
    assert run_baselines(feature_version, tmp_path / "out", policy, windows, batch_size=3) == output
    independent = run_baselines(
        feature_version, tmp_path / "independent", policy, windows, batch_size=3
    )
    independent_manifest = verify_baselines(independent)
    assert independent_manifest["baseline.json_sha256"] == manifest["baseline.json_sha256"]
    assert independent_manifest["evaluation.json_sha256"] == manifest["evaluation.json_sha256"]
    other_feature, other_policy, other_windows = _feature_fixture(
        tmp_path / "changed", validation_pressure=200.0
    )
    other_output = run_baselines(
        other_feature, tmp_path / "other", other_policy, other_windows, batch_size=3
    )
    other_artifact = json.loads((other_output / "baseline.json").read_text())
    assert artifact["robust_stats"] == other_artifact["robust_stats"]
    (output / "evaluation.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_baselines(output)


def test_wrong_feature_binding_rejected(tmp_path: Path) -> None:
    feature_version, policy, windows = _feature_fixture(tmp_path)
    data = json.loads(policy.read_text())
    data["feature_version_id"] = "wrong"
    policy.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="different feature version"):
        run_baselines(feature_version, tmp_path / "out", policy, windows)
    assert not list((tmp_path / "out").rglob("manifest.json"))
