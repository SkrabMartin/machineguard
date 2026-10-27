"""Synthetic four-subset benchmark artifact and test-truth isolation."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from machineguard.core.provenance import sha256_file
from machineguard.prognostics.benchmark import run_benchmark, verify_benchmark
from machineguard.prognostics.cmapss import SUBSETS


def _trajectory(unit: int, cycles: int, signal_shift: float = 0) -> str:
    rows = []
    for cycle in range(1, cycles + 1):
        values = [cycle, 1, 2, 3, *([float(cycle + unit) + signal_shift] * 21)]
        rows.append(" ".join(str(value) for value in [unit, *values]))
    return "\n".join(rows) + "\n"


def _fixture(
    tmp_path: Path,
    truth_shift: int = 0,
    shifted_validation_ids: dict[str, int] | None = None,
) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "synthetic-CMAPSSData.zip"
    counts = {}
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
        for subset in SUBSETS:
            train = "".join(
                _trajectory(
                    unit,
                    9 + unit,
                    signal_shift=100.0
                    if shifted_validation_ids and shifted_validation_ids.get(subset) == unit
                    else 0,
                )
                for unit in range(1, 7)
            )
            test = "".join(_trajectory(unit, 4 + unit) for unit in range(1, 4))
            zip_file.writestr(f"train_{subset}.txt", train)
            zip_file.writestr(f"test_{subset}.txt", test)
            zip_file.writestr(
                f"RUL_{subset}.txt", "".join(f"{value + truth_shift}\n" for value in (2, 4, 6))
            )
            counts[subset] = {
                "train_rows": sum(9 + unit for unit in range(1, 7)),
                "train_engines": 6,
                "test_rows": sum(4 + unit for unit in range(1, 4)),
                "test_engines": 3,
            }
    policy = json.loads(Path("configs/cmapss_rul_benchmark.json").read_text())
    policy["archive_sha256"] = sha256_file(archive)
    policy["subset_counts"] = counts
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    return archive, policy_path


def test_benchmark_is_reproducible_and_test_truth_is_isolated(tmp_path: Path) -> None:
    archive, policy = _fixture(tmp_path / "original")
    path = run_benchmark(archive, tmp_path / "out", policy)
    result = verify_benchmark(path)
    assert set(result["subsets"]) == set(SUBSETS)
    assert result["target"] == "uncapped remaining operational cycles at observed last cycle"
    for subset in SUBSETS:
        item = result["subsets"][subset]
        assert set(item["folds"]["fit_engine_ids"]).isdisjoint(
            item["folds"]["validation_engine_ids"]
        )
        assert item["validation"]["ridge"]["engines"] == 1
        assert item["publisher_test"]["ridge"]["engines"] == 3
        assert item["publisher_test"]["ridge"]["by_true_rul"]["rul_0_50"]["engines"] == 3
    assert run_benchmark(archive, tmp_path / "out", policy) == path
    second = run_benchmark(archive, tmp_path / "other", policy)
    assert (second / "report.json").read_bytes() == (path / "report.json").read_bytes()
    changed_archive, changed_policy = _fixture(tmp_path / "changed", truth_shift=10)
    changed = verify_benchmark(
        run_benchmark(changed_archive, tmp_path / "changed-out", changed_policy)
    )
    for subset in SUBSETS:
        assert result["subsets"][subset]["validation"] == changed["subsets"][subset]["validation"]
        assert (
            result["subsets"][subset]["ridge_coefficient_sha256"]
            == changed["subsets"][subset]["ridge_coefficient_sha256"]
        )
        assert (
            result["subsets"][subset]["publisher_test"]
            != changed["subsets"][subset]["publisher_test"]
        )
    (path / "report.json").write_text("tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_benchmark(path)


def test_wrong_archive_binding_rejected(tmp_path: Path) -> None:
    archive, policy = _fixture(tmp_path)
    data = json.loads(policy.read_text())
    data["archive_sha256"] = "0" * 64
    policy.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="SHA-256"):
        run_benchmark(archive, tmp_path / "out", policy)


def test_held_out_validation_features_do_not_change_fit(tmp_path: Path) -> None:
    archive, policy = _fixture(tmp_path / "original")
    original = verify_benchmark(run_benchmark(archive, tmp_path / "out", policy))
    validation_ids = {
        subset: item["folds"]["validation_engine_ids"][0]
        for subset, item in original["subsets"].items()
    }
    changed_archive, changed_policy = _fixture(
        tmp_path / "changed", shifted_validation_ids=validation_ids
    )
    changed = verify_benchmark(
        run_benchmark(changed_archive, tmp_path / "changed-out", changed_policy)
    )
    for subset in SUBSETS:
        assert (
            original["subsets"][subset]["ridge_coefficient_sha256"]
            == changed["subsets"][subset]["ridge_coefficient_sha256"]
        )
        assert (
            original["subsets"][subset]["publisher_test"]
            == changed["subsets"][subset]["publisher_test"]
        )
