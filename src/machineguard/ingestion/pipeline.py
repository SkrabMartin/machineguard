"""Strict MetroPT ingest with immutable source reference and atomic version publication."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from machineguard import __version__
from machineguard.core.catalog import METROPT3
from machineguard.core.provenance import SourceManifest, sha256_file
from machineguard.ingestion.metropt import InputValidationError, iter_metropt_rows
from machineguard.quality.metrics import QualityAccumulator, QualityPolicy
from machineguard.storage.parquet import MonthlyParquetWriter, canonical_schema, verify_dataset


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _version_id(source: SourceManifest, policy: QualityPolicy, source_format: str) -> str:
    identity = {
        "source_sha256": source.sha256,
        "dataset_id": METROPT3.dataset_id,
        "schema_version": METROPT3.schema_version,
        "parser_version": __version__,
        "source_format": source_format,
        "policy": asdict(policy),
    }
    return hashlib.sha256(_canonical_json(identity).encode()).hexdigest()[:24]


def ingest_metropt(
    source_path: Path,
    output_root: Path,
    source_manifest: SourceManifest,
    *,
    policy: QualityPolicy | None = None,
    batch_size: int = 50_000,
) -> Path:
    """Publish a new version, or verify and reuse an identical existing version."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    policy = policy or QualityPolicy()
    if source_manifest.dataset_id != METROPT3.dataset_id:
        raise ValueError("source manifest dataset ID does not match MetroPT-3")
    if source_manifest.schema_version != METROPT3.schema_version:
        raise ValueError("source manifest schema version does not match contract")
    if source_manifest.filename != source_path.name:
        raise ValueError("source manifest filename does not match input")
    if source_manifest.size_bytes != source_path.stat().st_size:
        raise ValueError("source size differs from manifest")
    if source_manifest.sha256 != sha256_file(source_path):
        raise ValueError("source SHA-256 differs from manifest")
    source_format = source_path.suffix.lower().lstrip(".")
    if source_format not in {"csv", "parquet"}:
        raise ValueError("MetroPT source must be CSV or Parquet")
    version_id = _version_id(source_manifest, policy, source_format)
    dataset_root = output_root / METROPT3.dataset_id
    final_dir = dataset_root / version_id
    if final_dir.exists():
        existing = verify_dataset(final_dir)
        if existing["source"] != json.loads(source_manifest.to_json()):
            raise ValueError("existing dataset has different source provenance")
        return final_dir
    dataset_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=dataset_root))
    writer = MonthlyParquetWriter(staging, canonical_schema(source_manifest.sha256), batch_size)
    quality = QualityAccumulator(policy)
    try:
        for data_number, row in enumerate(iter_metropt_rows(source_path, batch_size), start=1):
            try:
                quality.observe(row.event_time_source, row.measurements)
            except ValueError as exc:
                raise InputValidationError(data_number + 1, str(exc), data_number) from exc
            writer.append(row)
        if quality.rows_seen == 0:
            raise InputValidationError(2, "source has no data rows", 0)
        parts = writer.close()
        if (
            source_path.stat().st_size != source_manifest.size_bytes
            or sha256_file(source_path) != source_manifest.sha256
        ):
            raise ValueError("source changed during ingestion; version was not published")
        quality_data = quality.to_dict()
        quality_path = staging / "quality.json"
        quality_path.write_text(_canonical_json(quality_data) + "\n", encoding="utf-8")
        manifest: dict[str, Any] = {
            "version_id": version_id,
            "dataset_id": METROPT3.dataset_id,
            "schema_version": METROPT3.schema_version,
            "parser_version": __version__,
            "source_format": source_format,
            "source": json.loads(source_manifest.to_json()),
            "source_reference_only": True,
            "quality": quality_data,
            "quality_policy": asdict(policy),
            "quality_sha256": sha256_file(quality_path),
            "parts": [asdict(part) for part in parts],
            "published_at_utc": datetime.now(UTC).isoformat(),
        }
        (staging / "manifest.json").write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        if final_dir.exists():
            existing = verify_dataset(final_dir)
            if existing["source"] != json.loads(source_manifest.to_json()):
                raise ValueError("existing dataset has different source provenance")
            return final_dir
        try:
            staging.rename(final_dir)
        except OSError:
            if not final_dir.exists():
                raise
            existing = verify_dataset(final_dir)
            if existing["source"] != json.loads(source_manifest.to_json()):
                raise ValueError("existing dataset has different source provenance") from None
        return final_dir
    finally:
        writer.abort()
        if staging.exists():
            shutil.rmtree(staging)
