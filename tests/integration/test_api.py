"""Local HTTP contracts with explicitly synthetic source rows and artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from machineguard import __version__
from machineguard.api.app import create_app
from machineguard.api.catalog import Catalog, CatalogError
from machineguard.cli import main
from machineguard.core.catalog import METROPT3
from machineguard.core.provenance import SourceManifest, sha256_file
from machineguard.ingestion.pipeline import ingest_metropt


def _dataset(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "artifacts"
    source = tmp_path / "synthetic.csv"
    header = [field.raw_name for field in METROPT3.fields]
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        for index in range(4):
            writer.writerow(
                [str(index), f"2020-01-01 00:00:{index * 10:02d}", *([str(index + 1)] * 15)]
            )
    provenance = SourceManifest.from_file(
        source,
        dataset_id="metropt-3-uci",
        source_url="https://example.org/synthetic-test-source",
        retrieved_at_utc=datetime(2026, 10, 29, tzinfo=UTC),
        license_status="synthetic test fixture",
        schema_version="1",
        parser_version=__version__,
    )
    return root, ingest_metropt(source, root, provenance, batch_size=2)


def _baseline(root: Path) -> Path:
    path = root / "baselines" / ("a" * 24)
    path.mkdir(parents=True)
    evaluation = {
        "metrics": {"physical": {"validation": {"episodes": 0}}},
        "reported_windows": {"physical": []},
        "metric_caveat": "Synthetic descriptive alert counts; not a failure probability.",
    }
    (path / "evaluation.json").write_text(json.dumps(evaluation), encoding="utf-8")
    (path / "baseline.json").write_text("{}", encoding="utf-8")
    manifest = {
        "version_id": path.name,
        "identity": {"feature_manifest_sha256": "1" * 64},
        "baseline.json_sha256": sha256_file(path / "baseline.json"),
        "evaluation.json_sha256": sha256_file(path / "evaluation.json"),
    }
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _rul(root: Path) -> Path:
    identity = {"synthetic_fixture": True}
    version = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    path = root / "rul" / version
    path.mkdir(parents=True)
    report = {
        "identity": identity,
        "archive_sha256": "2" * 64,
        "target": "remaining operational cycles",
        "domain": "synthetic test fixture",
        "subsets": {"FD001": {"validation": {"mae": 1}, "publisher_test": {"mae": 2}}},
    }
    (path / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "version_id": version,
                "report_sha256": sha256_file(path / "report.json"),
                "archive_sha256": "2" * 64,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_catalog_persistence_pagination_tamper_and_schema(tmp_path: Path) -> None:
    root, dataset = _dataset(tmp_path)
    database = tmp_path / "catalog.sqlite"
    catalog = Catalog(root, database)
    record = catalog.register("dataset", dataset)
    assert catalog.register("dataset", dataset) == record
    assert Catalog(root, database).list(None, 0, 1)["total"] == 1
    assert catalog.list(None, 1, 1)["items"] == []
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(CatalogError, match="configured root"):
        catalog.register("dataset", outside)
    with pytest.raises(CatalogError, match="unknown artifact"):
        catalog.get("dataset", "0" * 24)
    (dataset / "quality.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(CatalogError, match="verification"):
        catalog.get("dataset", record["artifact_id"])
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(CatalogError, match="unsupported catalog schema"):
        Catalog(root, database)


def test_catalog_rejects_symlink_and_unknown_version_one_shape(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    artifact = root / "bad"
    artifact.mkdir()
    (artifact / "manifest.json").symlink_to(outside)
    catalog = Catalog(root, tmp_path / "catalog.sqlite")
    with pytest.raises(CatalogError, match="symlink"):
        catalog.register("baseline", artifact)
    with sqlite3.connect(tmp_path / "wrong.sqlite") as connection:
        connection.execute("CREATE TABLE artifacts (wrong TEXT)")
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(CatalogError, match="schema does not match"):
        Catalog(root, tmp_path / "wrong.sqlite")


def test_http_dataset_queries_and_limits(tmp_path: Path) -> None:
    root, dataset = _dataset(tmp_path)
    catalog = Catalog(root, tmp_path / "catalog.sqlite")
    artifact_id = catalog.register("dataset", dataset)["artifact_id"]
    client = TestClient(create_app(root, tmp_path / "catalog.sqlite"), client=("127.0.0.1", 50000))
    assert client.get("/api/v1/health").json()["scope"] == "local_research_only"
    assert client.get("/openapi.json").status_code == 200
    page = client.get("/api/v1/artifacts?limit=1").json()
    assert page["total"] == 1
    assert page["items"][0]["artifact_id"] == artifact_id
    assert "relative_path" not in page["items"][0]
    assert client.get("/api/v1/assets").json()["items"][0]["asset_id"] == "metropt-apu-1"
    quality = client.get(f"/api/v1/datasets/{artifact_id}/quality").json()
    assert quality["quality"]["rows_written"] == 4
    assert quality["provenance"]["manifest_sha256"] == sha256_file(dataset / "manifest.json")
    endpoint = f"/api/v1/datasets/{artifact_id}/series"
    params = {
        "start": "2020-01-01T00:00:00",
        "end": "2020-01-01T00:01:00",
        "sensors": "tp3,motor_current",
        "limit": 2,
    }
    first = client.get(endpoint, params=params).json()
    assert [item["source_row_index"] for item in first["items"]] == [0, 1]
    assert first["next_offset"] == 2
    assert first["sensor_units"] == {"tp3": "bar", "motor_current": "A"}
    second = client.get(endpoint, params={**params, "offset": 2}).json()
    assert [item["source_row_index"] for item in second["items"]] == [2, 3]
    assert second["next_offset"] is None
    for changes in (
        {"limit": 501},
        {"sensors": "unknown"},
        {"start": "2020-01-01T00:00:00+00:00"},
        {"end": "2020-01-09T00:00:00"},
    ):
        assert client.get(endpoint, params={**params, **changes}).status_code == 422
    assert client.get(f"/api/v1/datasets/{'0' * 24}/quality").status_code == 404
    (dataset / "quality.json").write_text("tampered", encoding="utf-8")
    assert client.get(f"/api/v1/datasets/{artifact_id}/quality").status_code == 409


def test_alert_rul_unavailable_scenario_and_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    baseline = _baseline(root)
    rul = _rul(root)
    database = tmp_path / "catalog.sqlite"
    assert (
        main(
            [
                "index-api-artifact",
                "baseline",
                str(baseline),
                "--root",
                str(root),
                "--database",
                str(database),
            ]
        )
        == 0
    )
    baseline_id = json.loads(capsys.readouterr().out)["artifact_id"]
    rul_id = Catalog(root, database).register("rul", rul)["artifact_id"]
    client = TestClient(create_app(root, database), client=("127.0.0.1", 50000))
    alerts = client.get(f"/api/v1/baselines/{baseline_id}/alerts").json()
    assert alerts["evidence_status"] == "descriptive_anomaly_only"
    assert alerts["metrics"]["physical"]["validation"]["episodes"] == 0
    result = client.get(f"/api/v1/rul/{rul_id}").json()
    assert result["transfer_status"] == "not_validated_for_metropt_or_industrial_compressors"
    assert "folds" not in result["subsets"]["FD001"]
    assert client.get("/api/v1/assets/metropt-apu-1/risk").json()["status"] == "unavailable"
    assert client.get("/api/v1/assets/metropt-apu-1/rul").json()["status"] == "unavailable"
    scenario = json.loads(Path("configs/maintenance_scenario.synthetic.json").read_text())
    response = client.post("/api/v1/scenarios/evaluate", json=scenario)
    assert response.status_code == 200
    assert response.json()["evidence_status"] == "hypothetical_only"
    scenario["assets"][0]["anomaly_score"] = "0.8"
    assert client.post("/api/v1/scenarios/evaluate", json=scenario).status_code == 422
    scenario["scenario_kind"] = "calibrated_model"
    assert client.post("/api/v1/scenarios/evaluate", json=scenario).status_code == 422
    assert client.get("/api/v1/artifacts?limit=101").status_code == 422


def test_api_rejects_remote_clients_and_oversized_body(tmp_path: Path) -> None:
    application = create_app(tmp_path / "root", tmp_path / "catalog.sqlite")
    remote = TestClient(application, client=("198.51.100.7", 40000))
    response = remote.get("/api/v1/health")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_only"
    local = TestClient(application, client=("127.0.0.1", 50000))
    too_large = local.post("/api/v1/scenarios/evaluate", content=b"x" * 65537)
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "request_too_large"
    chunked = local.post(
        "/api/v1/scenarios/evaluate",
        content=iter([b"{}"]),
        headers={"transfer-encoding": "chunked"},
    )
    assert chunked.status_code == 411
