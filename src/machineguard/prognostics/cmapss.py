"""Strict parser for NASA classic C-MAPSS trajectories and terminal truth."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import IO
from zipfile import ZipFile

import numpy as np
from numpy.typing import NDArray

SUBSETS = ("FD001", "FD002", "FD003", "FD004")
FEATURE_NAMES = (
    "cycle",
    *(f"setting_{i}" for i in range(1, 4)),
    *(f"sensor_{i}" for i in range(1, 22)),
)


@dataclass(frozen=True, slots=True)
class Trajectory:
    unit_id: int
    features: NDArray[np.float64]

    @property
    def lifetime_observed(self) -> int:
        return int(self.features[-1, 0])


def _positive_integer(token: str, field: str) -> int:
    if not token.isascii() or not token.isdecimal() or int(token) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return int(token)


def read_trajectories(stream: IO[bytes]) -> tuple[tuple[Trajectory, ...], str, int]:
    """Parse ordered engines with contiguous cycles and finite 26-column rows."""
    digest = hashlib.sha256()
    trajectories: list[Trajectory] = []
    unit_id = 0
    feature_rows: list[list[float]] = []
    row_count = 0
    for line_number, raw in enumerate(stream, start=1):
        digest.update(raw)
        try:
            fields = raw.decode("ascii").split()
        except UnicodeDecodeError as exc:
            raise ValueError(f"trajectory line {line_number} is not ASCII") from exc
        if len(fields) != 26:
            raise ValueError(f"trajectory line {line_number} must contain 26 columns")
        observed_id = _positive_integer(fields[0], "unit ID")
        cycle = _positive_integer(fields[1], "cycle")
        if observed_id != unit_id:
            if unit_id:
                trajectories.append(Trajectory(unit_id, np.asarray(feature_rows, dtype=np.float64)))
            if observed_id != unit_id + 1 or cycle != 1:
                raise ValueError("engine IDs and first cycles must be contiguous and ordered")
            unit_id = observed_id
            feature_rows = []
        if cycle != len(feature_rows) + 1:
            raise ValueError(f"unit {unit_id} has duplicate or missing cycle")
        try:
            values = [float(value) for value in fields[2:]]
        except ValueError as exc:
            raise ValueError(f"trajectory line {line_number} has nonnumeric signal") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"trajectory line {line_number} has nonfinite signal")
        feature_rows.append([float(cycle), *values])
        row_count += 1
    if unit_id:
        trajectories.append(Trajectory(unit_id, np.asarray(feature_rows, dtype=np.float64)))
    if not trajectories:
        raise ValueError("trajectory file is empty")
    return tuple(trajectories), digest.hexdigest(), row_count


def read_terminal_truth(stream: IO[bytes], engine_count: int) -> tuple[NDArray[np.int64], str]:
    """Truth line i belongs to test engine i; zero remaining cycles is valid."""
    digest = hashlib.sha256()
    values: list[int] = []
    for line_number, raw in enumerate(stream, start=1):
        digest.update(raw)
        try:
            tokens = raw.decode("ascii").split()
        except UnicodeDecodeError as exc:
            raise ValueError(f"truth line {line_number} is not ASCII") from exc
        if len(tokens) != 1 or not tokens[0].isascii() or not tokens[0].isdecimal():
            raise ValueError(f"truth line {line_number} must contain one nonnegative integer")
        values.append(int(tokens[0]))
    if len(values) != engine_count:
        raise ValueError("truth vector length differs from test engine count")
    return np.asarray(values, dtype=np.int64), digest.hexdigest()


def read_member(zip_file: ZipFile, name: str) -> tuple[tuple[Trajectory, ...], str, int]:
    with zip_file.open(name) as stream:
        return read_trajectories(stream)
