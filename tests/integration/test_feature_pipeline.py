"""Versioned synthetic feature build and provenance checks."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from machineguard import __version__
from machineguard.core.catalog import METROPT3
from machineguard.core.provenance import SourceManifest
from machineguard.features.pipeline import build_features, verify_features
from machineguard.ingestion.pipeline import ingest_metropt
from machineguard.storage.parquet import verify_dataset


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "synthetic.csv"
    header = [item.raw_name for item in METROPT3.fields]
    rows = []
    for index, time, pressure, load in [
        (0, "2020-02-01 00:00:00", 2, 0),
        (1, "2020-02-01 00:00:10", 4, 1),
        (2, "2020-02-01 00:00:20", 6, 1),
        (3, "2020-02-01 01:00:00", 8, 0),
        (4, "2020-03-01 01:00:10", 10, 1),
    ]:
        values = {name: "1" for name in header[2:]}
        values["TP3"] = str(pressure)
        values["DV_eletric"] = str(load)
        rows.append([str(index), time, *(values[name] for name in header[2:])])
    with source.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)
    manifest = SourceManifest.from_file(
        source,
        dataset_id="metropt-3-uci",
        source_url="https://example.org/synthetic",
        retrieved_at_utc=datetime(2026, 10, 27, tzinfo=UTC),
        license_status="synthetic test fixture",
        schema_version="1",
        parser_version=__version__,
    )
    canonical = ingest_metropt(source, tmp_path / "canonical", manifest, batch_size=2)
    source_info = verify_dataset(canonical)
    split = {
        "dataset_version_id": canonical.name,
        "source_sha256": source_info["source"]["sha256"],
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
    split_path = tmp_path / "split.json"
    split_path.write_text(json.dumps(split))
    windows = {
        "evidence_type": "reported_fault_window",
        "not_row_label": True,
        "windows": [
            {
                "start": "2020-02-01T00:00:10",
                "end_inclusive_minute": "2020-02-01T00:00:00",
                "description": "synthetic report",
            }
        ],
    }
    windows_path = tmp_path / "windows.json"
    windows_path.write_text(json.dumps(windows))
    return canonical, Path("configs/metropt_feature_spec.json"), split_path, windows_path


def test_feature_build_verify_reuse_and_tamper(tmp_path: Path) -> None:
    canonical, spec, split, windows = _fixture(tmp_path)
    output = build_features(canonical, tmp_path / "features", spec, split, windows, batch_size=2)
    manifest = verify_features(output)
    assert manifest["rows"] == 5
    assert manifest["split_counts"] == {
        "train": 2,
        "validation": 0,
        "test": 1,
        "purge": 2,
        "outside": 0,
    }
    assert len(manifest["parts"]) == 2
    first_rows = pq.read_table(output / manifest["parts"][0]["path"]).to_pylist()
    assert first_rows[0]["tp3_mean_5m"] is None
    assert first_rows[1]["tp3_lag1"] == 2.0
    assert first_rows[2]["tp3_mean_5m"] == pytest.approx(4.0)
    assert first_rows[2]["dv_electric_transition"] == 0
    assert first_rows[2]["split"] == "purge"
    eda = json.loads((output / "eda.json").read_text())
    assert eda["reported_fault_windows"][0]["rows_in_window"] == 2
    assert eda["states"]["dv_electric"] == {"0.0": 2, "1.0": 3}
    assert (
        build_features(canonical, tmp_path / "features", spec, split, windows, batch_size=2)
        == output
    )
    other = build_features(canonical, tmp_path / "other", spec, split, windows, batch_size=2)
    assert [part["sha256"] for part in manifest["parts"]] == [
        part["sha256"] for part in verify_features(other)["parts"]
    ]
    part = output / manifest["parts"][0]["path"]
    with part.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_features(output)
    with pytest.raises(ValueError, match="hash mismatch"):
        build_features(canonical, tmp_path / "features", spec, split, windows)


def test_source_split_mismatch_cannot_publish(tmp_path: Path) -> None:
    canonical, spec, split, windows = _fixture(tmp_path)
    data = json.loads(split.read_text())
    data["source_sha256"] = "0" * 64
    split.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="does not match"):
        build_features(canonical, tmp_path / "features", spec, split, windows)
    assert not list((tmp_path / "features").rglob("manifest.json"))
