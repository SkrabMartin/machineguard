"""Immutable source schema contracts for Phase 1 file-level verification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TimeBasis(StrEnum):
    WALL_CLOCK = "wall_clock"
    CYCLES = "cycles"


class FieldRole(StrEnum):
    INDEX = "index"
    TIMESTAMP = "timestamp"
    ASSET_ID = "asset_id"
    CYCLE = "cycle"
    SENSOR = "sensor"
    SETTING = "setting"


class FieldType(StrEnum):
    INTEGER = "integer"
    FLOAT = "float"
    DATETIME = "datetime"


@dataclass(frozen=True, slots=True)
class FieldContract:
    raw_name: str
    canonical_name: str
    role: FieldRole
    dtype: FieldType
    unit: str | None = None

    def __post_init__(self) -> None:
        if not self.canonical_name or not self.canonical_name.isidentifier():
            raise ValueError("canonical_name must be a nonempty Python identifier")
        if self.raw_name == "" and self.role is not FieldRole.INDEX:
            raise ValueError("Only an unnamed source index may have an empty raw_name")
        if self.role in {FieldRole.INDEX, FieldRole.ASSET_ID, FieldRole.CYCLE}:
            if self.dtype is not FieldType.INTEGER:
                raise ValueError("index, asset ID, and cycle fields must be integers")
        elif self.role is FieldRole.TIMESTAMP:
            if self.dtype is not FieldType.DATETIME:
                raise ValueError("timestamp fields must be datetimes")
        elif self.dtype is not FieldType.FLOAT:
            raise ValueError("sensor and setting fields must be floats")
        if self.unit is not None and not self.unit.strip():
            raise ValueError("unit must be meaningful or None")


@dataclass(frozen=True, slots=True)
class DatasetContract:
    dataset_id: str
    schema_version: str
    source_format: str
    fields: tuple[FieldContract, ...]
    time_basis: TimeBasis
    time_field: str
    asset_field: str | None
    timestamp_format: str | None
    timezone_policy: str | None
    has_header: bool
    label_status: str

    def __post_init__(self) -> None:
        if not self.dataset_id or not self.schema_version:
            raise ValueError("dataset_id and schema_version are required")
        if self.source_format not in {"csv", "whitespace"}:
            raise ValueError("source_format must be csv or whitespace")
        if not self.fields:
            raise ValueError("at least one field is required")
        raw = [field.raw_name for field in self.fields]
        canonical = [field.canonical_name for field in self.fields]
        if len(raw) != len(set(raw)) or len(canonical) != len(set(canonical)):
            raise ValueError("field names must be unique")
        if self.time_field not in canonical:
            raise ValueError("time_field must be a canonical field")
        if self.asset_field is not None and self.asset_field not in canonical:
            raise ValueError("asset_field must be a canonical field")
        time_role = next(
            field.role for field in self.fields if field.canonical_name == self.time_field
        )
        if self.time_basis is TimeBasis.WALL_CLOCK:
            if time_role is not FieldRole.TIMESTAMP or self.timestamp_format is None:
                raise ValueError("wall-clock contracts need a timestamp field and format")
            if self.timezone_policy is None:
                raise ValueError("wall-clock contracts need an explicit timezone policy")
        elif time_role is not FieldRole.CYCLE or self.timestamp_format is not None:
            raise ValueError("cycle contracts need a cycle field and no timestamp format")
        if self.has_header != (self.source_format == "csv"):
            raise ValueError("CSV contracts need headers; whitespace contracts do not")
        if not self.label_status.strip():
            raise ValueError("label_status is required")

    def validate_header(self, observed: tuple[str, ...]) -> None:
        """Check order and names only; row validation belongs to Phase 2."""
        if not self.has_header:
            raise ValueError("This source has no header")
        expected = tuple(field.raw_name for field in self.fields)
        if observed != expected:
            raise ValueError(f"Header mismatch: expected {expected!r}, observed {observed!r}")

    def validate_width(self, observed: int) -> None:
        if observed != len(self.fields):
            raise ValueError(f"Expected {len(self.fields)} fields, observed {observed}")
