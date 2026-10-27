"""Strict, bounded readers for the observed MetroPT-3 source layout."""

from __future__ import annotations

import csv
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from machineguard.core.catalog import METROPT3
from machineguard.core.contracts import FieldRole


@dataclass(frozen=True, slots=True)
class ParsedMetroptRow:
    source_row_index: int
    event_time_source: datetime
    measurements: dict[str, float]


class InputValidationError(ValueError):
    def __init__(self, row_number: int, reason: str, rows_seen: int) -> None:
        self.row_number = row_number
        self.reason = reason
        self.rows_seen = rows_seen
        self.rejected_rows = 1
        super().__init__(f"source row {row_number}: {reason} (rows seen: {rows_seen})")


def _parse_row(values: Sequence[object], row_number: int) -> ParsedMetroptRow:
    try:
        METROPT3.validate_width(len(values))
        raw_index = values[0]
        if isinstance(raw_index, bool) or not isinstance(raw_index, (str, int)):
            raise ValueError("source index is missing or invalid")
        index = int(raw_index)
        if str(index) != str(raw_index).strip():
            raise ValueError("source index is not an integer token")
        if index < 0:
            raise ValueError("source index cannot be negative")
        timestamp_value = values[1]
        if isinstance(timestamp_value, datetime):
            timestamp = timestamp_value
        elif isinstance(timestamp_value, str):
            timestamp = datetime.strptime(timestamp_value, METROPT3.timestamp_format or "")
        else:
            raise ValueError("timestamp has an invalid type")
        if timestamp.tzinfo is not None:
            raise ValueError("source timestamp must be timezone-naive until source zone is known")
        if timestamp.microsecond != 0:
            raise ValueError("source timestamp must have whole-second precision")
        measurements: dict[str, float] = {}
        for field, raw in zip(METROPT3.fields[2:], values[2:], strict=True):
            if isinstance(raw, bool) or not isinstance(raw, (str, float, int)) or raw == "":
                raise ValueError(f"{field.raw_name} is missing or invalid")
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError(f"{field.raw_name} is not finite")
            measurements[field.canonical_name] = number
        return ParsedMetroptRow(index, timestamp, measurements)
    except (TypeError, ValueError) as exc:
        raise InputValidationError(row_number, str(exc), max(0, row_number - 1)) from exc


def _csv_rows(path: Path) -> Iterator[ParsedMetroptRow]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source, strict=True)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise InputValidationError(1, "CSV is empty", 0) from exc
            try:
                METROPT3.validate_header(tuple(header))
            except ValueError as exc:
                raise InputValidationError(1, str(exc), 0) from exc
            try:
                for row_number, values in enumerate(reader, start=2):
                    yield _parse_row(values, row_number)
            except csv.Error as exc:
                raise InputValidationError(
                    reader.line_num, f"CSV parse error: {exc}", reader.line_num
                ) from exc
    except UnicodeError as exc:
        raise InputValidationError(0, f"invalid UTF-8: {exc}", 0) from exc


def _validate_raw_parquet_schema(schema: pa.Schema) -> None:
    METROPT3.validate_header(tuple(schema.names))
    for source_field, arrow_field in zip(METROPT3.fields, schema, strict=True):
        if source_field.role is FieldRole.INDEX and not pa.types.is_integer(arrow_field.type):
            raise ValueError("source index must be an integer Parquet field")
        if source_field.role is FieldRole.TIMESTAMP and not (
            pa.types.is_timestamp(arrow_field.type) or pa.types.is_string(arrow_field.type)
        ):
            raise ValueError("timestamp must be a string or timestamp Parquet field")
        if source_field.role is FieldRole.SENSOR and not pa.types.is_floating(arrow_field.type):
            raise ValueError(f"{source_field.raw_name} must be a floating Parquet field")


def _parquet_rows(path: Path, batch_size: int) -> Iterator[ParsedMetroptRow]:
    try:
        parquet = pq.ParquetFile(path)
    except pa.ArrowException as exc:
        raise InputValidationError(0, f"corrupt Parquet source: {exc}", 0) from exc
    try:
        try:
            _validate_raw_parquet_schema(parquet.schema_arrow)
        except ValueError as exc:
            raise InputValidationError(0, f"Parquet schema: {exc}", 0) from exc
        row_number = 1
        try:
            for batch in parquet.iter_batches(batch_size=batch_size):
                columns = [column.to_pylist() for column in batch.columns]
                for values in zip(*columns, strict=True):
                    row_number += 1
                    yield _parse_row(values, row_number)
        except pa.ArrowException as exc:
            raise InputValidationError(
                row_number, f"corrupt Parquet source: {exc}", row_number - 1
            ) from exc
    finally:
        parquet.close()


def iter_metropt_rows(path: Path, batch_size: int = 50_000) -> Iterator[ParsedMetroptRow]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        yield from _csv_rows(path)
    elif suffix == ".parquet":
        yield from _parquet_rows(path, batch_size)
    else:
        raise ValueError("MetroPT source must have .csv or .parquet extension")
