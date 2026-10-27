"""Bounded feature materialization with source lineage and verified immutable outputs."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from machineguard import __version__
from machineguard.core.provenance import sha256_file
from machineguard.features.eda import EDAAccumulator, load_report_windows
from machineguard.features.engine import FeatureEngine, FeatureInput, FeatureOutput
from machineguard.features.spec import FeatureSpec, definitions
from machineguard.features.splits import SplitPlan
from machineguard.storage.parquet import iter_canonical_batches, verify_dataset


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def feature_schema(source_sha256: str, spec: FeatureSpec) -> pa.Schema:
    fields = [
        pa.field("asset_id", pa.string(), nullable=False),
        pa.field("source_row_index", pa.int64(), nullable=False),
        pa.field("event_time_source", pa.timestamp("ms"), nullable=False),
        pa.field("split", pa.string(), nullable=False),
    ]
    for item in definitions(spec):
        kind = pa.int64() if item.unit in ("flag", "count", "s") else pa.float64()
        fields.append(
            pa.field(
                item.name,
                kind,
                nullable=item.min_samples > 1
                or item.name in ("operating_load_flag", "loaded_run_elapsed_capped_s"),
            )
        )
    metadata = {
        b"machineguard.feature_spec": _json(spec.to_dict()).encode(),
        b"machineguard.feature_definitions": _json(
            [asdict(item) for item in definitions(spec)]
        ).encode(),
        b"machineguard.source_sha256": source_sha256.encode(),
        b"machineguard.timezone_policy": b"source_naive_unresolved",
        b"machineguard.phase": b"3",
    }
    return pa.schema(fields, metadata=metadata)


class _Writer:
    def __init__(self, root: Path, schema: pa.Schema, batch_size: int) -> None:
        self.root = root
        self.schema = schema
        self.batch_size = batch_size
        self._month: tuple[int, int] | None = None
        self._writer: pq.ParquetWriter | None = None
        self._path: Path | None = None
        self._buffer: dict[str, list[Any]] = {name: [] for name in schema.names}
        self._part_rows = 0
        self.parts: list[dict[str, Any]] = []

    def append(self, output: FeatureOutput, split: str) -> None:
        month = (output.event_time_source.year, output.event_time_source.month)
        if month != self._month:
            self._close()
            path = (
                self.root / f"year={month[0]:04d}" / f"month={month[1]:02d}" / "part-00000.parquet"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            self._path = path
            self._writer = pq.ParquetWriter(path, self.schema, compression="zstd")
            self._month = month
        values = asdict(output)
        values["split"] = split
        for name in self.schema.names:
            self._buffer[name].append(values[name])
        self._part_rows += 1
        if len(self._buffer["asset_id"]) >= self.batch_size:
            self._flush()

    def _flush(self) -> None:
        if self._writer is None or not self._buffer["asset_id"]:
            return
        self._writer.write_table(pa.Table.from_pydict(self._buffer, schema=self.schema))
        self._buffer = {name: [] for name in self.schema.names}

    def _close(self) -> None:
        if self._writer is None or self._path is None:
            return
        self._flush()
        self._writer.close()
        self.parts.append(
            {
                "path": self._path.relative_to(self.root).as_posix(),
                "rows": self._part_rows,
                "sha256": sha256_file(self._path),
            }
        )
        self._writer = None
        self._path = None
        self._month = None
        self._part_rows = 0

    def close(self) -> list[dict[str, Any]]:
        self._close()
        return self.parts

    def abort(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None


def _verify_features(version_dir: Path, expected_version_id: str) -> dict[str, Any]:
    """Check all output hashes, row counts, schema metadata and provenance."""
    try:
        manifest: dict[str, Any] = json.loads((version_dir / "manifest.json").read_text())
        if manifest["version_id"] != expected_version_id:
            raise ValueError("feature version ID mismatch")
        if sha256_file(version_dir / "eda.json") != manifest["eda_sha256"]:
            raise ValueError("EDA hash mismatch")
        spec = FeatureSpec(**manifest["feature_spec"])
        expected = feature_schema(manifest["source_sha256"], spec)
        total = 0
        for part in manifest["parts"]:
            path = (version_dir / part["path"]).resolve()
            if not path.is_relative_to(version_dir.resolve()) or path.suffix != ".parquet":
                raise ValueError("unsafe feature part path")
            if sha256_file(path) != part["sha256"]:
                raise ValueError("feature part hash mismatch")
            parquet = pq.ParquetFile(path)
            try:
                if not parquet.schema_arrow.equals(expected, check_metadata=True):
                    raise ValueError("feature schema mismatch")
                if parquet.metadata.num_rows != part["rows"]:
                    raise ValueError("feature row count mismatch")
            finally:
                parquet.close()
            total += part["rows"]
        if total != manifest["rows"] or total != sum(manifest["split_counts"].values()):
            raise ValueError("feature total row count mismatch")
        return manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError, pa.ArrowException) as exc:
        raise ValueError(f"invalid feature dataset: {exc}") from exc


def verify_features(version_dir: Path) -> dict[str, Any]:
    return _verify_features(version_dir, version_dir.name)


def build_features(
    input_version: Path,
    output_root: Path,
    spec_path: Path,
    split_path: Path,
    windows_path: Path,
    *,
    batch_size: int = 10_000,
) -> Path:
    """Build one deterministic version; never alter canonical input."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    source = verify_dataset(input_version)
    spec = FeatureSpec.from_json_file(spec_path)
    split = SplitPlan.from_json_file(split_path)
    windows = load_report_windows(windows_path)
    if (
        split.dataset_version_id != source["version_id"]
        or split.source_sha256 != source["source"]["sha256"]
    ):
        raise ValueError("split plan does not match canonical source version")
    if split.max_feature_lookback_seconds < spec.long_window_seconds:
        raise ValueError("split lookback is shorter than feature window")
    identity = {
        "source_manifest_sha256": sha256_file(input_version / "manifest.json"),
        "spec": spec.to_dict(),
        "split": split.to_dict(),
        "report_windows_sha256": sha256_file(windows_path),
        "builder_version": __version__,
        "builder_code_sha256": hashlib.sha256(
            "".join(
                sha256_file(Path(__file__).with_name(name))
                for name in ("engine.py", "spec.py", "splits.py", "eda.py", "pipeline.py")
            ).encode()
        ).hexdigest(),
    }
    version_id = hashlib.sha256(_json(identity).encode()).hexdigest()[:24]
    root = output_root / "metropt-3-uci" / version_id
    if root.exists():
        existing = verify_features(root)
        if existing["identity"] != identity:
            raise ValueError("feature version identity mismatch")
        return root
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root.parent))
    writer = _Writer(staging, feature_schema(split.source_sha256, spec), batch_size)
    engine = FeatureEngine(spec)
    eda = EDAAccumulator(windows)
    counts = {name: 0 for name in ("train", "validation", "test", "purge", "outside")}
    try:
        for batch in iter_canonical_batches(input_version, batch_size=batch_size):
            for row in batch.to_pylist():
                eda.observe(row)
                feature_input = FeatureInput(
                    **{name: row[name] for name in FeatureInput.__dataclass_fields__}
                )
                output = engine.process(feature_input)
                assigned = split.assign(output.event_time_source)
                writer.append(output, assigned)
                counts[assigned] += 1
        parts = writer.close()
        if eda.rows != source["quality"]["rows_written"]:
            raise ValueError("input row count changed during feature build")
        eda_data = eda.to_dict()
        eda_data["canonical_quality"] = source["quality"]
        (staging / "eda.json").write_text(_json(eda_data) + "\n", encoding="utf-8")
        manifest = {
            "version_id": version_id,
            "identity": identity,
            "source_version_id": source["version_id"],
            "source_sha256": split.source_sha256,
            "source_timezone_policy": split.timezone_policy,
            "feature_spec": spec.to_dict(),
            "feature_definitions": [asdict(item) for item in definitions(spec)],
            "split_plan": split.to_dict(),
            "split_counts": counts,
            "report_window_semantics": "descriptive source reports; not row labels",
            "rows": eda.rows,
            "parts": parts,
            "eda_sha256": sha256_file(staging / "eda.json"),
            "published_at_utc": datetime.now(UTC).isoformat(),
        }
        (staging / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
        _verify_features(staging, version_id)
        if root.exists():
            verify_features(root)
            return root
        staging.rename(root)
        verify_features(root)
        return root
    finally:
        writer.abort()
        if staging.exists():
            shutil.rmtree(staging)
