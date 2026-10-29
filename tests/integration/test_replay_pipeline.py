"""Synthetic canonical telemetry replay, restart, and batch parity."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from machineguard import __version__
from machineguard.anomaly.pipeline import run_baselines
from machineguard.core.catalog import METROPT3
from machineguard.core.provenance import SourceManifest
from machineguard.features.pipeline import build_features
from machineguard.ingestion.pipeline import ingest_metropt
from machineguard.replay.engine import ReplayPolicy
from machineguard.replay.pipeline import (
    run_replay,
    verify_replay_parity,
    verify_replay_report,
)
from machineguard.storage.parquet import verify_dataset


def _fixture(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    source = root / "synthetic-metropt.csv"
    header = [item.raw_name for item in METROPT3.fields]
    rows = [
        (0, "2020-02-01 00:00:00", 2.0),
        (1, "2020-02-01 00:00:10", 4.0),
        (2, "2020-02-01 00:00:20", 6.0),
        (3, "2020-02-01 00:00:20", 8.0),
        (4, "2020-02-01 01:00:00", 10.0),
        (5, "2020-03-01 01:00:00", 12.0),
        (6, "2020-03-01 01:00:10", 14.0),
        (7, "2020-03-01 01:00:20", 16.0),
    ]
    with source.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        for index, stamp, pressure in rows:
            values = {name: "1" for name in header[2:]}
            values["TP3"] = str(pressure)
            writer.writerow([index, stamp, *(values[name] for name in header[2:])])
    provenance = SourceManifest.from_file(
        source,
        dataset_id="metropt-3-uci",
        source_url="https://example.org/synthetic",
        retrieved_at_utc=datetime(2026, 10, 29, tzinfo=UTC),
        license_status="synthetic test fixture",
        schema_version="1",
        parser_version=__version__,
    )
    canonical = ingest_metropt(source, root / "canonical", provenance, batch_size=2)
    manifest = verify_dataset(canonical)
    split = {
        "dataset_version_id": canonical.name,
        "source_sha256": manifest["source"]["sha256"],
        "train_start": "2020-02-01T00:00:00",
        "train_end": "2020-02-01T00:00:20",
        "validation_start": "2020-02-01T01:00:20",
        "validation_end": "2020-03-01T00:00:00",
        "test_start": "2020-03-01T01:00:00",
        "test_end": "2020-03-02T00:00:00",
        "purge_seconds": 3600,
        "max_feature_lookback_seconds": 3600,
        "prediction_horizon_seconds": 0,
        "usage": "anomaly_descriptive_only",
        "timezone_policy": "source_naive_unresolved",
    }
    split_path = root / "split.json"
    split_path.write_text(json.dumps(split))
    windows = {
        "evidence_type": "reported_fault_window",
        "not_row_label": True,
        "windows": [
            {
                "start": "2020-02-01T00:00:10",
                "end_inclusive_minute": "2020-02-01T00:00:00",
                "description": "synthetic event",
            }
        ],
    }
    windows_path = root / "windows.json"
    windows_path.write_text(json.dumps(windows))
    spec = Path("configs/metropt_feature_spec.json")
    features = build_features(
        canonical, root / "features", spec, split_path, windows_path, batch_size=2
    )
    return canonical, features, spec, split_path, windows_path


def test_replay_restart_trace_integrity_and_batch_parity(tmp_path: Path) -> None:
    canonical, features, spec, split, _ = _fixture(tmp_path)
    policy = ReplayPolicy(max_lateness_seconds=0, checkpoint_every=2)
    partial = run_replay(
        canonical, tmp_path / "replayed", spec, split, policy, max_arrivals=3, batch_size=2
    )
    checkpoint = json.loads((partial / "checkpoint.json").read_text())["payload"]
    assert checkpoint["complete"] is False
    assert checkpoint["engine"]["decisions"] == 3
    assert verify_replay_report(partial)["status"] == "partial_checkpointed"
    assert verify_replay_parity(partial, features)["checked_rows"] == 3
    with (partial / "decisions.jsonl").open("ab") as stream:
        stream.write(b"orphan-after-checkpoint\n")
    resumed = run_replay(canonical, tmp_path / "replayed", spec, split, policy, batch_size=2)
    assert resumed == partial
    report = verify_replay_report(resumed)
    assert report["decisions"] == 8
    assert report["sampling_gaps"] >= 1
    assert verify_replay_parity(resumed, features)["checked_rows"] == 8
    independent = run_replay(canonical, tmp_path / "independent", spec, split, policy, batch_size=3)
    assert (resumed / "decisions.jsonl").read_bytes() == (
        independent / "decisions.jsonl"
    ).read_bytes()
    assert run_replay(canonical, tmp_path / "replayed", spec, split, policy) == resumed
    with (resumed / "decisions.jsonl").open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_replay_report(resumed)


def test_replay_mismatched_split_rejected(tmp_path: Path) -> None:
    canonical, _, spec, split, _ = _fixture(tmp_path)
    data = json.loads(split.read_text())
    data["source_sha256"] = "0" * 64
    split.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="does not match canonical"):
        run_replay(canonical, tmp_path / "replayed", spec, split, ReplayPolicy())


def test_replay_score_parity_with_synthetic_baseline(tmp_path: Path) -> None:
    canonical, features, spec, split, windows = _fixture(tmp_path)
    policy = json.loads(Path("configs/metropt_anomaly_baseline.json").read_text())
    policy["feature_version_id"] = features.name
    policy["min_group_samples"] = 2
    policy["report_exclusion_seconds"] = 0
    policy_path = tmp_path / "baseline-policy.json"
    policy_path.write_text(json.dumps(policy))
    baseline = run_baselines(features, tmp_path / "baselines", policy_path, windows, batch_size=2)
    replay = run_replay(
        canonical,
        tmp_path / "replayed",
        spec,
        split,
        ReplayPolicy(checkpoint_every=2),
        baseline_dir=baseline,
        baseline_policy_path=policy_path,
        batch_size=2,
    )
    parity = verify_replay_parity(replay, features)
    assert parity["checked_rows"] == 8
    assert parity["scores_checked"] is True
    decisions = [json.loads(line) for line in (replay / "decisions.jsonl").read_text().splitlines()]
    assert decisions[-1]["scores"]["physical"]["eligible"] is True
