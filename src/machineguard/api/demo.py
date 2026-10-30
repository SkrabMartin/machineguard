"""Small deterministic synthetic telemetry bundle for the isolated public demo."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from bisect import bisect_left
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from machineguard.api.catalog import CatalogError
from machineguard.core.provenance import sha256_file
from machineguard.storage.parquet import canonical_schema, iter_canonical_batches, verify_dataset

DATASET_ID = "machineguard-synthetic-demo-v1"
DATA_ORIGIN = "synthetic_demo"
ASSETS = ("synthetic-compressor-a", "synthetic-compressor-b")
ROW_COUNT = 1440
START = datetime(2020, 2, 1)
INTERVAL_SECONDS = 10
GENERATOR_VERSION = "1"
MAX_PART_BYTES = 1_000_000


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _spec(asset_id: str) -> dict[str, object]:
    return {
        "dataset_id": DATASET_ID,
        "data_origin": DATA_ORIGIN,
        "asset_id": asset_id,
        "generator_version": GENERATOR_VERSION,
        "start_synthetic_time": START.isoformat(),
        "interval_seconds": INTERVAL_SECONDS,
        "rows": ROW_COUNT,
    }


def _identity(asset_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(_json_bytes(_spec(asset_id))).hexdigest()
    return digest[:24], digest


def _measurements(index: int, asset_number: int) -> dict[str, float]:
    """Integer-derived patterns; these do not imitate observed fault events."""
    triangle = min(index % 180, 180 - index % 180) / 90
    pressure = 8.0 + 0.4 * asset_number + 1.5 * triangle
    stopped = index % 400 < 20
    return {
        "tp2": round(pressure + 1.2, 6),
        "tp3": round(pressure, 6),
        "h1": round(pressure - 0.6, 6),
        "dv_pressure": round(1.0 + 0.2 * triangle, 6),
        "reservoirs": round(pressure + 0.7, 6),
        "oil_temperature": round(57 + asset_number * 3 + (index % 240) / 120, 6),
        "motor_current": round(4.5 + asset_number * 0.3 + (index % 70) / 100, 6),
        "comp": float(not stopped),
        "dv_electric": float(stopped),
        "towers": 1.0,
        "mpg": 1.0,
        "lps": 0.0,
        "pressure_switch": float(pressure > 8.5),
        "oil_level": 1.0,
        "caudal_impulses": float((index // 10) % 5),
    }


def _quality() -> dict[str, object]:
    return {
        "rows_written": ROW_COUNT,
        "sampling_interval_seconds": INTERVAL_SECONDS,
        "generated_missing_samples": 0,
        "data_origin": DATA_ORIGIN,
        "note": "Complete artificial signals; no observed faults or model labels",
    }


def initialize_demo(root: Path) -> dict[str, Any]:
    """Create one immutable demo bundle; refuse an existing incompatible directory."""
    if root.exists() or root.is_symlink():
        catalog = DemoCatalog(root)
        return {"root": str(catalog.root), "artifacts": catalog.list(None, 0, 100)["items"]}
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".machineguard-demo-", dir=root.parent))
    try:
        records: list[dict[str, Any]] = []
        for asset_number, asset_id in enumerate(ASSETS):
            version_id, source_sha = _identity(asset_id)
            target = staging / "artifacts" / version_id
            target.mkdir(parents=True)
            schema = canonical_schema(source_sha, DATASET_ID, "synthetic_naive")
            columns: dict[str, list[Any]] = {name: [] for name in schema.names}
            for index in range(ROW_COUNT):
                row: dict[str, Any] = {
                    "asset_id": asset_id,
                    "source_row_index": index,
                    "event_time_source": START + timedelta(seconds=index * INTERVAL_SECONDS),
                    **_measurements(index, asset_number),
                }
                for name in schema.names:
                    columns[name].append(row[name])
            part = target / "data.parquet"
            pq.write_table(
                pa.Table.from_pydict(columns, schema=schema),
                part,
                compression="zstd",
                row_group_size=360,
            )
            if part.stat().st_size > MAX_PART_BYTES:
                raise ValueError("generated demo part exceeds size bound")
            quality = _quality()
            (target / "quality.json").write_bytes(_json_bytes(quality))
            manifest = {
                "version_id": version_id,
                "dataset_id": DATASET_ID,
                "schema_version": "1",
                "data_origin": DATA_ORIGIN,
                "time_basis": "synthetic_naive",
                "generator_version": GENERATOR_VERSION,
                "source": {**_spec(asset_id), "sha256": source_sha},
                "quality": quality,
                "quality_sha256": sha256_file(target / "quality.json"),
                "parts": [{"path": "data.parquet", "rows": ROW_COUNT, "sha256": sha256_file(part)}],
            }
            (target / "manifest.json").write_bytes(_json_bytes(manifest))
            records.append(
                {
                    "artifact_id": version_id,
                    "kind": "dataset",
                    "relative_path": f"artifacts/{version_id}",
                    "manifest_sha256": sha256_file(target / "manifest.json"),
                    "summary": {
                        "asset_id": asset_id,
                        "dataset_id": DATASET_ID,
                        "source_sha256": source_sha,
                        "data_origin": DATA_ORIGIN,
                        "time_basis": "synthetic_naive",
                        "rows": ROW_COUNT,
                    },
                }
            )
        (staging / "catalog.json").write_bytes(
            _json_bytes(
                {
                    "schema_version": 1,
                    "profile": "public_synthetic_demo",
                    "generator_version": GENERATOR_VERSION,
                    "artifacts": records,
                }
            )
        )
        DemoCatalog(staging)
        staging.rename(root)
    finally:
        if staging.exists():
            import shutil

            shutil.rmtree(staging)
    catalog = DemoCatalog(root)
    return {"root": str(catalog.root), "artifacts": catalog.list(None, 0, 100)["items"]}


class DemoCatalog:
    """Read-only, isolated two-asset catalog with cached rows and hash checks."""

    def __init__(self, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise CatalogError("public demo root is missing or is a symlink")
        self.root = root.resolve()
        if {item.name for item in self.root.iterdir()} != {"catalog.json", "artifacts"}:
            raise CatalogError("public demo root contains unexpected files")
        if any(item.is_symlink() for item in self.root.rglob("*")):
            raise CatalogError("public demo contains a symlink")
        try:
            catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
            if set(catalog) != {"schema_version", "profile", "generator_version", "artifacts"}:
                raise CatalogError("public demo catalog fields changed")
            if (
                catalog["schema_version"] != 1
                or catalog["profile"] != "public_synthetic_demo"
                or catalog["generator_version"] != GENERATOR_VERSION
                or not isinstance(catalog["artifacts"], list)
                or len(catalog["artifacts"]) != len(ASSETS)
            ):
                raise CatalogError("public demo catalog identity mismatch")
            self._catalog_sha = sha256_file(self.root / "catalog.json")
            self._records: dict[str, dict[str, Any]] = {}
            self._rows: dict[str, tuple[dict[str, Any], ...]] = {}
            self._times: dict[str, tuple[datetime, ...]] = {}
            versions = {_identity(asset)[0] for asset in ASSETS}
            if {item.name for item in (self.root / "artifacts").iterdir()} != versions:
                raise CatalogError("public demo artifact set mismatch")
            sensor_names = tuple(_measurements(0, 0))
            for asset_number, (asset_id, entry) in enumerate(
                zip(ASSETS, catalog["artifacts"], strict=True)
            ):
                version_id, source_sha = _identity(asset_id)
                expected_summary = {
                    "asset_id": asset_id,
                    "dataset_id": DATASET_ID,
                    "source_sha256": source_sha,
                    "data_origin": DATA_ORIGIN,
                    "time_basis": "synthetic_naive",
                    "rows": ROW_COUNT,
                }
                if (
                    set(entry)
                    != {"artifact_id", "kind", "relative_path", "manifest_sha256", "summary"}
                    or entry["artifact_id"] != version_id
                    or entry["kind"] != "dataset"
                    or entry["relative_path"] != f"artifacts/{version_id}"
                    or entry["summary"] != expected_summary
                ):
                    raise CatalogError("public demo artifact identity mismatch")
                path = self.root / entry["relative_path"]
                if {item.name for item in path.iterdir()} != {
                    "manifest.json",
                    "quality.json",
                    "data.parquet",
                }:
                    raise CatalogError("public demo artifact contains unexpected files")
                manifest = self._verify_entry(entry)
                if manifest["source"] != {**_spec(asset_id), "sha256": source_sha}:
                    raise CatalogError("public demo source provenance mismatch")
                if manifest["quality"] != _quality():
                    raise CatalogError("public demo quality report mismatch")
                rows: list[dict[str, Any]] = []
                for batch in iter_canonical_batches(path, batch_size=360):
                    rows.extend(batch.to_pylist())
                times = tuple(row["event_time_source"] for row in rows)
                if (
                    len(rows) != ROW_COUNT
                    or any(row["asset_id"] != asset_id for row in rows)
                    or any(row["source_row_index"] != i for i, row in enumerate(rows))
                    or any(
                        value != START + timedelta(seconds=i * INTERVAL_SECONDS)
                        for i, value in enumerate(times)
                    )
                    or any(
                        {name: row[name] for name in sensor_names} != _measurements(i, asset_number)
                        for i, row in enumerate(rows)
                    )
                ):
                    raise CatalogError("public demo rows do not match generator contract")
                self._records[version_id] = entry
                self._rows[version_id] = tuple(rows)
                self._times[version_id] = times
        except (OSError, ValueError, KeyError, TypeError, pa.ArrowException) as exc:
            raise CatalogError(f"public demo verification failed: {exc}") from exc

    def _verify_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        path = self.root / entry["relative_path"]
        if path.is_symlink() or any(item.is_symlink() for item in path.rglob("*")):
            raise CatalogError("public demo contains a symlink")
        if (path / "data.parquet").stat().st_size > MAX_PART_BYTES:
            raise CatalogError("public demo part exceeds size bound")
        if sha256_file(path / "manifest.json") != entry["manifest_sha256"]:
            raise CatalogError("public demo manifest changed")
        manifest = verify_dataset(path)
        if (
            manifest.get("data_origin") != DATA_ORIGIN
            or manifest.get("dataset_id") != DATASET_ID
            or manifest.get("time_basis") != "synthetic_naive"
            or manifest["quality"]["rows_written"] != ROW_COUNT
        ):
            raise CatalogError("public demo data contract mismatch")
        return manifest

    def list(self, kind: str | None, offset: int, limit: int) -> dict[str, Any]:
        if kind not in (None, "dataset", "baseline", "rul") or offset < 0 or not 1 <= limit <= 100:
            raise CatalogError("invalid demo catalog query")
        if sha256_file(self.root / "catalog.json") != self._catalog_sha:
            raise CatalogError("public demo catalog changed")
        entries = list(self._records.values()) if kind in (None, "dataset") else []
        return {
            "items": [
                {key: value for key, value in entry.items() if key != "relative_path"}
                for entry in entries[offset : offset + limit]
            ],
            "offset": offset,
            "limit": limit,
            "total": len(entries),
        }

    def get(self, kind: str, artifact_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        if kind != "dataset" or artifact_id not in self._records:
            raise CatalogError("unknown artifact")
        entry = self._records[artifact_id]
        try:
            manifest = self._verify_entry(entry)
        except (OSError, ValueError, KeyError, TypeError, pa.ArrowException) as exc:
            raise CatalogError(f"public demo artifact failed verification: {exc}") from exc
        return (
            self.root / entry["relative_path"],
            manifest,
            {key: value for key, value in entry.items() if key != "relative_path"},
        )

    def series(
        self,
        artifact_id: str,
        start: datetime,
        end: datetime,
        sensors: Sequence[str],
        offset: int,
        limit: int,
    ) -> tuple[Sequence[dict[str, Any]], int | None]:
        times = self._times[artifact_id]
        rows = self._rows[artifact_id]
        first = bisect_left(times, start)
        last = bisect_left(times, end)
        selected = rows[first + offset : min(first + offset + limit, last)]
        items = [
            {
                "asset_id": row["asset_id"],
                "source_row_index": row["source_row_index"],
                "event_time_source": row["event_time_source"].isoformat(),
                "measurements": {name: row[name] for name in sensors},
            }
            for row in selected
        ]
        next_offset = offset + limit if first + offset + limit < last else None
        return items, next_offset


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize isolated synthetic MachineGuard demo")
    parser.add_argument("--root", required=True, type=Path)
    arguments = parser.parse_args()
    print(json.dumps(initialize_demo(arguments.root), sort_keys=True))


if __name__ == "__main__":
    main()
