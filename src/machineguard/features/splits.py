"""Chronological anomaly split with a measured causal-feature purge gap."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

SplitName = Literal["train", "validation", "test", "purge", "outside"]


def _parse_local(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is not None:
        raise ValueError("split boundaries must use timezone-naive source time")
    return result


@dataclass(frozen=True, slots=True)
class SplitPlan:
    dataset_version_id: str
    source_sha256: str
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    test_start: datetime
    test_end: datetime
    purge_seconds: int
    max_feature_lookback_seconds: int
    prediction_horizon_seconds: int
    usage: str
    timezone_policy: str = "source_naive_unresolved"

    def __post_init__(self) -> None:
        boundaries = (
            self.train_start,
            self.train_end,
            self.validation_start,
            self.validation_end,
            self.test_start,
            self.test_end,
        )
        if any(value.tzinfo is not None for value in boundaries):
            raise ValueError("split boundaries must be timezone-naive")
        if not (
            self.train_start
            < self.train_end
            <= self.validation_start
            < self.validation_end
            <= self.test_start
            < self.test_end
        ):
            raise ValueError("split boundaries must be chronological and nonempty")
        if self.prediction_horizon_seconds != 0 or self.usage != "anomaly_descriptive_only":
            raise ValueError("Phase 3 split is for descriptive/anomaly work, not risk targets")
        if self.purge_seconds < self.max_feature_lookback_seconds:
            raise ValueError("purge must cover maximum feature lookback")
        if self.validation_start - self.train_end < timedelta(seconds=self.purge_seconds):
            raise ValueError("train/validation purge gap is too short")
        if self.test_start - self.validation_end < timedelta(seconds=self.purge_seconds):
            raise ValueError("validation/test purge gap is too short")
        if self.timezone_policy != "source_naive_unresolved":
            raise ValueError("source timezone policy must remain unresolved")
        if len(self.source_sha256) != 64 or not self.dataset_version_id:
            raise ValueError("source hash and dataset version ID are required")

    @classmethod
    def from_json_file(cls, path: Path) -> SplitPlan:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("split plan fields do not match the schema")
        for name in (
            "train_start",
            "train_end",
            "validation_start",
            "validation_end",
            "test_start",
            "test_end",
        ):
            data[name] = _parse_local(data[name])
        return cls(**data)

    def to_dict(self) -> dict[str, object]:
        return {
            name: value.isoformat() if isinstance(value, datetime) else value
            for name, value in asdict(self).items()
        }

    def assign(self, event_time_source: datetime) -> SplitName:
        if event_time_source.tzinfo is not None:
            raise ValueError("feature event time must be timezone-naive")
        if self.train_start <= event_time_source < self.train_end:
            return "train"
        if self.validation_start <= event_time_source < self.validation_end:
            return "validation"
        if self.test_start <= event_time_source < self.test_end:
            return "test"
        if self.train_end <= event_time_source < self.validation_start:
            return "purge"
        if self.validation_end <= event_time_source < self.test_start:
            return "purge"
        return "outside"
