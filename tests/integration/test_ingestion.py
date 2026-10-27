"""Synthetic MetroPT-shaped sources; no real publisher rows are committed."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from machineguard import __version__
from machineguard.cli import main
from machineguard.core.catalog import METROPT3
from machineguard.core.provenance import SourceManifest
from machineguard.ingestion.metropt import InputValidationError
from machineguard.ingestion.pipeline import ingest_metropt
from machineguard.storage.parquet import iter_canonical_batches, verify_dataset

HEADER = [field.raw_name for field in METROPT3.fields]


def _row(index: int, timestamp: str, **overrides: str) -> list[str]:
    values = {name: "1.0" for name in HEADER[2:]}
    values.update(overrides)
    return [str(index), timestamp, *(values[name] for name in HEADER[2:])]


def _csv(path: Path, rows: list[list[str]], header: list[str] | None = None) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADER if header is None else header)
        writer.writerows(rows)


def _manifest(path: Path) -> SourceManifest:
    return SourceManifest.from_file(
        path,
        dataset_id="metropt-3-uci",
        source_url="https://example.org/synthetic-test-source",
        retrieved_at_utc=datetime(2026, 10, 27, tzinfo=UTC),
        license_status="synthetic test fixture",
        schema_version="1",
        parser_version=__version__,
    )


def test_csv_month_partitions_quality_and_idempotence(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.csv"
    _csv(
        source,
        [
            _row(0, "2020-01-31 23:59:50"),
            _row(10, "2020-02-01 00:00:00"),
            _row(11, "2020-02-01 00:00:00"),
            _row(20, "2020-02-01 00:01:00", TP2="40", COMP="2"),
        ],
    )
    root = tmp_path / "processed"
    version = ingest_metropt(source, root, _manifest(source), batch_size=2)
    report = verify_dataset(version)
    quality = report["quality"]
    assert quality["rows_seen"] == quality["rows_written"] == 4
    assert quality["rejected_rows"] == 0
    assert quality["duplicate_timestamps"] == 1
    assert quality["gap_warnings"] == 1
    assert quality["max_gap_seconds"] == 60
    assert quality["interval_buckets"] == {"0": 1, "10": 1, "21-60": 1}
    assert quality["plausibility_counts"] == {"comp": 1, "tp2": 1}
    assert [part["rows"] for part in report["parts"]] == [1, 3]
    assert [part["path"] for part in report["parts"]] == [
        "year=2020/month=01/part-00000.parquet",
        "year=2020/month=02/part-00000.parquet",
    ]
    batches = list(iter_canonical_batches(version, batch_size=2))
    assert sum(batch.num_rows for batch in batches) == 4
    assert batches[0].schema.metadata[b"machineguard.timezone_policy"] == b"source_naive_unresolved"
    units = json.loads(batches[0].schema.metadata[b"machineguard.units_json"])
    assert units["tp2"] == "bar"
    assert units["oil_temperature"] == "degC"
    before = (version / "manifest.json").read_bytes()
    assert ingest_metropt(source, root, _manifest(source), batch_size=2) == version
    assert (version / "manifest.json").read_bytes() == before
    assert not list((root / "metropt-3-uci").glob(".staging-*"))


@pytest.mark.parametrize(
    ("rows", "header", "message"),
    [
        ([_row(0, "2020-01-01 00:00:00")], HEADER[:-1], "Header mismatch"),
        ([_row(0, "2020-01-01 00:00:00", TP2="")], None, "TP2 is missing"),
        ([_row(0, "2020-01-01 00:00:00", TP2="nan")], None, "not finite"),
        ([_row(0, "2020-01-01 00:00:00")[:-1]], None, "Expected 17"),
        ([_row(0, "2020-01-01 00:00:00"), _row(1, "2019-12-31 23:59:00")], None, "nondecreasing"),
        ([], None, "no data rows"),
    ],
)
def test_invalid_csv_aborts_without_publication(
    tmp_path: Path, rows: list[list[str]], header: list[str] | None, message: str
) -> None:
    source = tmp_path / "synthetic.csv"
    _csv(source, rows, header)
    root = tmp_path / "processed"
    with pytest.raises(InputValidationError, match=message) as error:
        ingest_metropt(source, root, _manifest(source), batch_size=1)
    assert error.value.rejected_rows == 1
    assert source.exists()
    assert not list(root.rglob("manifest.json"))
    assert not list(root.rglob(".staging-*"))


def test_raw_parquet_import_and_schema_rejection(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.parquet"
    columns: list[pa.Array] = [pa.array([0, 10], type=pa.int64())]
    columns.append(pa.array(["2020-01-01 00:00:00", "2020-01-01 00:00:10"]))
    columns.extend(pa.array([1.0, 2.0], type=pa.float64()) for _ in HEADER[2:])
    table = pa.Table.from_arrays(columns, names=HEADER)
    pq.write_table(table, source)
    version = ingest_metropt(source, tmp_path / "processed", _manifest(source), batch_size=1)
    assert verify_dataset(version)["quality"]["rows_written"] == 2
    assert sum(batch.num_rows for batch in iter_canonical_batches(version)) == 2
    bad = tmp_path / "bad.parquet"
    pq.write_table(table.rename_columns([*HEADER[:-1], "wrong"]), bad)
    with pytest.raises(InputValidationError, match="Parquet schema"):
        ingest_metropt(bad, tmp_path / "processed", _manifest(bad))


def test_corrupt_existing_version_is_not_replaced(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.csv"
    _csv(source, [_row(0, "2020-01-01 00:00:00")])
    root = tmp_path / "processed"
    version = ingest_metropt(source, root, _manifest(source))
    (version / "quality.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        ingest_metropt(source, root, _manifest(source))
    assert (version / "quality.json").read_text() == "tampered"


def test_corrupt_part_hash_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.csv"
    _csv(source, [_row(0, "2020-01-01 00:00:00")])
    version = ingest_metropt(source, tmp_path / "processed", _manifest(source))
    part = version / verify_dataset(version)["parts"][0]["path"]
    with part.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="part hash mismatch"):
        verify_dataset(version)


def test_source_manifest_mismatch_rejected(tmp_path: Path) -> None:
    from dataclasses import replace

    source = tmp_path / "synthetic.csv"
    _csv(source, [_row(0, "2020-01-01 00:00:00")])
    with pytest.raises(ValueError, match="SHA-256 differs"):
        ingest_metropt(
            source,
            tmp_path / "processed",
            replace(_manifest(source), sha256="0" * 64),
        )
    assert not (tmp_path / "processed").exists()


def test_source_change_before_publication_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "synthetic.csv"
    _csv(source, [_row(0, "2020-01-01 00:00:00")])
    from machineguard.storage.parquet import MonthlyParquetWriter

    original = MonthlyParquetWriter.close

    def change_source(self: MonthlyParquetWriter) -> tuple[object, ...]:
        result = original(self)
        with source.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        return result

    monkeypatch.setattr(MonthlyParquetWriter, "close", change_source)
    root = tmp_path / "processed"
    with pytest.raises(ValueError, match="source changed during ingestion"):
        ingest_metropt(source, root, _manifest(source))
    assert not list(root.rglob("manifest.json"))
    assert not list(root.rglob(".staging-*"))


def test_writer_failure_removes_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "synthetic.csv"
    _csv(source, [_row(0, "2020-01-01 00:00:00"), _row(1, "2020-01-01 00:00:10")])
    from machineguard.storage.parquet import MonthlyParquetWriter

    original = MonthlyParquetWriter.append
    calls = 0

    def fail_second(self: MonthlyParquetWriter, row: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic writer failure")
        original(self, row)  # type: ignore[arg-type]

    monkeypatch.setattr(MonthlyParquetWriter, "append", fail_second)
    root = tmp_path / "processed"
    with pytest.raises(RuntimeError, match="synthetic writer failure"):
        ingest_metropt(source, root, _manifest(source), batch_size=1)
    assert not list(root.rglob("manifest.json"))
    assert not list(root.rglob(".staging-*"))


def test_cli_manifest_ingest_inspect(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "synthetic.csv"
    _csv(source, [_row(0, "2020-01-01 00:00:00")])
    assert (
        main(
            [
                "source-manifest",
                str(source),
                "--source-url",
                "https://example.org/synthetic",
                "--license-status",
                "synthetic fixture",
                "--retrieved-at-utc",
                "2026-10-27T00:00:00+00:00",
            ]
        )
        == 0
    )
    manifest = tmp_path / "source.json"
    manifest.write_text(capsys.readouterr().out, encoding="utf-8")
    assert (
        main(
            [
                "ingest-metropt",
                str(source),
                "--source-manifest",
                str(manifest),
                "--output-root",
                str(tmp_path / "processed"),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["quality"]["rows_written"] == 1
    assert main(["inspect-dataset", output["version_dir"]]) == 0
    assert json.loads(capsys.readouterr().out)["quality"]["rows_written"] == 1
