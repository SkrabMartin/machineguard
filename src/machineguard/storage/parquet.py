"""Bounded monthly Parquet writer and verified canonical reader."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from machineguard.core.catalog import METROPT3
from machineguard.core.provenance import sha256_file
from machineguard.ingestion.metropt import ParsedMetroptRow

ASSET_ID = "metropt-apu-1"


def canonical_schema(source_sha256: str) -> pa.Schema:
    fields = [
        pa.field("asset_id", pa.string(), nullable=False),
        pa.field("source_row_index", pa.int64(), nullable=False),
        pa.field("event_time_source", pa.timestamp("ms"), nullable=False),
        *(
            pa.field(field.canonical_name, pa.float64(), nullable=False)
            for field in METROPT3.fields[2:]
        ),
    ]
    units = {field.canonical_name: field.unit for field in METROPT3.fields[2:]}
    metadata = {
        b"machineguard.dataset_id": METROPT3.dataset_id.encode(),
        b"machineguard.schema_version": METROPT3.schema_version.encode(),
        b"machineguard.source_sha256": source_sha256.encode(),
        b"machineguard.timezone_policy": b"source_naive_unresolved",
        b"machineguard.units_json": json.dumps(units, sort_keys=True).encode(),
    }
    return pa.schema(fields, metadata=metadata)


@dataclass(frozen=True, slots=True)
class PartRecord:
    path: str
    rows: int
    sha256: str


class MonthlyParquetWriter:
    """At most one open month and one row batch are held in memory."""

    def __init__(self, staging_root: Path, schema: pa.Schema, batch_size: int) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.root = staging_root
        self.schema = schema
        self.batch_size = batch_size
        self._month: tuple[int, int] | None = None
        self._path: Path | None = None
        self._writer: pq.ParquetWriter | None = None
        self._buffer: dict[str, list[Any]] = {name: [] for name in schema.names}
        self._part_rows = 0
        self.parts: list[PartRecord] = []

    def append(self, row: ParsedMetroptRow) -> None:
        month = (row.event_time_source.year, row.event_time_source.month)
        if self._month != month:
            self._close_month()
            self._open_month(month)
        self._buffer["asset_id"].append(ASSET_ID)
        self._buffer["source_row_index"].append(row.source_row_index)
        self._buffer["event_time_source"].append(row.event_time_source)
        for name, value in row.measurements.items():
            self._buffer[name].append(value)
        self._part_rows += 1
        if len(self._buffer["asset_id"]) >= self.batch_size:
            self._flush()

    def _open_month(self, month: tuple[int, int]) -> None:
        year, number = month
        self._month = month
        self._path = self.root / f"year={year:04d}" / f"month={number:02d}" / "part-00000.parquet"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._writer = pq.ParquetWriter(self._path, self.schema, compression="zstd")
        self._part_rows = 0

    def _flush(self) -> None:
        if not self._buffer["asset_id"]:
            return
        if self._writer is None:
            raise RuntimeError("cannot flush without an open writer")
        table = pa.Table.from_pydict(self._buffer, schema=self.schema)
        self._writer.write_table(table)
        self._buffer = {name: [] for name in self.schema.names}

    def _close_month(self) -> None:
        if self._writer is None or self._path is None:
            return
        self._flush()
        self._writer.close()
        self.parts.append(
            PartRecord(
                path=self._path.relative_to(self.root).as_posix(),
                rows=self._part_rows,
                sha256=sha256_file(self._path),
            )
        )
        self._writer = None
        self._path = None
        self._month = None
        self._part_rows = 0

    def close(self) -> tuple[PartRecord, ...]:
        self._close_month()
        return tuple(self.parts)

    def abort(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None


def _validated_part_path(version_dir: Path, relative: str) -> Path:
    path = (version_dir / relative).resolve()
    if not path.is_relative_to(version_dir.resolve()) or path.suffix != ".parquet":
        raise ValueError("unsafe part path in manifest")
    return path


def verify_dataset(version_dir: Path) -> dict[str, Any]:
    manifest_path = version_dir / "manifest.json"
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["version_id"] != version_dir.name:
            raise ValueError("version ID mismatch")
        if sha256_file(version_dir / "quality.json") != manifest["quality_sha256"]:
            raise ValueError("quality report hash mismatch")
        total_rows = 0
        for part in manifest["parts"]:
            path = _validated_part_path(version_dir, part["path"])
            if sha256_file(path) != part["sha256"]:
                raise ValueError(f"part hash mismatch: {part['path']}")
            parquet = pq.ParquetFile(path)
            try:
                rows = parquet.metadata.num_rows
            finally:
                parquet.close()
            if rows != part["rows"]:
                raise ValueError(f"part row count mismatch: {part['path']}")
            total_rows += rows
        if total_rows != manifest["quality"]["rows_written"]:
            raise ValueError("dataset row count mismatch")
        return manifest
    except (KeyError, TypeError, json.JSONDecodeError, OSError, pa.ArrowException) as exc:
        raise ValueError(f"invalid dataset manifest or file: {exc}") from exc


def iter_canonical_batches(version_dir: Path, batch_size: int = 50_000) -> Iterator[pa.RecordBatch]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    manifest = verify_dataset(version_dir)
    expected = canonical_schema(manifest["source"]["sha256"])
    for part in manifest["parts"]:
        parquet = pq.ParquetFile(_validated_part_path(version_dir, part["path"]))
        try:
            if not parquet.schema_arrow.equals(expected, check_metadata=True):
                raise ValueError(f"canonical schema mismatch: {part['path']}")
            yield from parquet.iter_batches(batch_size=batch_size)
        finally:
            parquet.close()
