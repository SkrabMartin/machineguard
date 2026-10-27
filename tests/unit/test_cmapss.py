"""Synthetic C-MAPSS parsing, split, and target checks."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import numpy as np
import pytest

from machineguard.prognostics.benchmark import (
    BenchmarkPolicy,
    engine_group_split,
    validation_cutoff,
)
from machineguard.prognostics.cmapss import Trajectory, read_terminal_truth, read_trajectories


def _row(unit: int, cycle: int, value: str = "1") -> bytes:
    return f"{unit} {cycle} ".encode() + (f"{value} " * 24).encode() + b"\n"


def test_parser_accepts_ordered_cycles_and_zero_truth() -> None:
    stream = BytesIO(_row(1, 1) + _row(1, 2) + _row(2, 1))
    engines, digest, count = read_trajectories(stream)
    assert count == 3
    assert len(digest) == 64
    assert [item.lifetime_observed for item in engines] == [2, 1]
    truth, _ = read_terminal_truth(BytesIO(b"0\n7\n"), len(engines))
    assert truth.tolist() == [0, 7]


@pytest.mark.parametrize(
    "content, message",
    [
        (_row(1, 1) + _row(1, 1), "duplicate or missing"),
        (_row(1, 1) + _row(3, 1), "contiguous and ordered"),
        (_row(1, 1) + _row(2, 2), "contiguous and ordered"),
        (b"1 1 2\n", "26 columns"),
        (_row(1, 1, "nan"), "nonfinite"),
        (b"1.5 1 " + b"1 " * 24 + b"\n", "positive integer"),
    ],
)
def test_parser_rejects_invalid_trajectories(content: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        read_trajectories(BytesIO(content))


def test_truth_alignment_rejects_missing_or_extra_values() -> None:
    with pytest.raises(ValueError, match="length"):
        read_terminal_truth(BytesIO(b"3\n"), 2)
    with pytest.raises(ValueError, match="nonnegative integer"):
        read_terminal_truth(BytesIO(b"-1\n"), 1)


def test_engine_split_and_cutoff_are_deterministic_and_isolated() -> None:
    engines = tuple(
        Trajectory(unit, np.asarray([[float(cycle), *([1.0] * 24)] for cycle in range(1, 11)]))
        for unit in range(1, 11)
    )
    fit, validation = engine_group_split(engines, 17, "FD001", 0.2)
    assert len(fit) == 8 and len(validation) == 2
    assert {x.unit_id for x in fit}.isdisjoint({x.unit_id for x in validation})
    assert engine_group_split(engines, 17, "FD001", 0.2) == (fit, validation)
    policy = BenchmarkPolicy.from_file(Path("configs/cmapss_rul_benchmark.json"))
    for engine in validation:
        cutoff = validation_cutoff(engine, policy, "FD001")
        assert 1 <= cutoff < engine.lifetime_observed
        assert engine.lifetime_observed - cutoff > 0


def test_policy_rejects_bad_fraction(tmp_path: Path) -> None:
    import json

    data = json.loads(Path("configs/cmapss_rul_benchmark.json").read_text())
    data["validation_fraction"] = 1.0
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="validation"):
        BenchmarkPolicy.from_file(path)
    data = json.loads(Path("configs/cmapss_rul_benchmark.json").read_text())
    data["target_policy"] = "capped at 125 cycles"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="identity or policy"):
        BenchmarkPolicy.from_file(path)
