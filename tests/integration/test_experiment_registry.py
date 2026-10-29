"""Synthetic registry tests; no third-party dataset bytes are distributed."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

from machineguard.core.provenance import sha256_file
from machineguard.experiments.registry import (
    compare_runs,
    list_runs,
    read_run,
    register_run,
    reproduce_run,
)
from machineguard.prognostics.benchmark import run_benchmark
from machineguard.prognostics.cmapss import SUBSETS


def _inputs(root: Path, *, truth_shift: int = 0, seed: int = 17) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    archive = root / "synthetic-CMAPSSData.zip"
    counts = {}
    with ZipFile(archive, "w") as bundle:

        def write_member(name: str, content: str) -> None:
            bundle.writestr(ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), content)

        for subset in SUBSETS:
            train_rows = []
            test_rows = []
            for unit in range(1, 7):
                for cycle in range(1, 10 + unit):
                    train_rows.append(
                        " ".join(
                            str(value) for value in (unit, cycle, 1, 2, 3, *([cycle + unit] * 21))
                        )
                    )
            for unit in range(1, 4):
                for cycle in range(1, 5 + unit):
                    test_rows.append(
                        " ".join(
                            str(value) for value in (unit, cycle, 1, 2, 3, *([cycle + unit] * 21))
                        )
                    )
            write_member(f"train_{subset}.txt", "\n".join(train_rows) + "\n")
            write_member(f"test_{subset}.txt", "\n".join(test_rows) + "\n")
            write_member(
                f"RUL_{subset}.txt", f"{2 + truth_shift}\n{4 + truth_shift}\n{6 + truth_shift}\n"
            )
            counts[subset] = {
                "train_rows": len(train_rows),
                "train_engines": 6,
                "test_rows": len(test_rows),
                "test_engines": 3,
            }
    policy = json.loads(Path("configs/cmapss_rul_benchmark.json").read_text())
    policy["archive_sha256"] = sha256_file(archive)
    policy["subset_counts"] = counts
    policy["seed"] = seed
    policy_path = root / "policy.json"
    policy_path.write_text(json.dumps(policy))
    return archive, policy_path


def _registered(
    root: Path, *, truth_shift: int = 0, seed: int = 17
) -> tuple[dict[str, object], Path, Path]:
    archive, policy = _inputs(root / "source", truth_shift=truth_shift, seed=seed)
    artifact = run_benchmark(archive, root / "artifacts", policy)
    registry = root / "registry"
    record = register_run(registry, "cmapss-rul", artifact, {"archive": archive, "policy": policy})
    return record, registry, archive


def test_registry_round_trip_and_exact_reproduction(tmp_path: Path) -> None:
    record, registry, archive = _registered(tmp_path)
    run_id = str(record["run_id"])
    assert read_run(registry, run_id) == record
    assert list_runs(registry) == [record]
    assert record["promotion_status"] == "research_only"
    assert reproduce_run(registry, run_id, tmp_path / "reproduced")["status"] == "reproduced"
    archive.unlink()
    blocked = reproduce_run(registry, run_id, tmp_path / "later")
    assert blocked["status"] == "blocked"
    assert "archive: missing bytes" in blocked["reasons"][0]


def test_registry_rejects_tampering_and_incompatible_inputs(tmp_path: Path) -> None:
    record, registry, archive = _registered(tmp_path / "first")
    run_id = str(record["run_id"])
    second, second_registry, _ = _registered(tmp_path / "second", truth_shift=5)
    other = read_run(second_registry, str(second["run_id"]))
    with pytest.raises(ValueError, match="SHA-256"):
        register_run(
            registry,
            "cmapss-rul",
            Path(str(record["artifact_dir"])),
            {"archive": archive, "policy": tmp_path / "second" / "source" / "policy.json"},
        )
    assert other["metrics"] != record["metrics"]
    record_path = registry / "runs" / run_id / "record.json"
    payload = json.loads(record_path.read_text())
    payload["promotion_status"] = "promoted"
    record_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="record hash mismatch"):
        read_run(registry, run_id)


def test_recovery_and_compatibility_gate(tmp_path: Path) -> None:
    record, registry, _ = _registered(tmp_path / "first")
    staging = registry / "runs" / ".staging-abandoned"
    staging.mkdir()
    assert list_runs(registry) == [record]
    second, _, _ = _registered(tmp_path / "second")
    archive = Path(second["inputs"]["archive"]["path"])
    policy = Path(second["inputs"]["policy"]["path"])
    second_artifact = Path(str(second["artifact_dir"]))
    added = register_run(
        registry, "cmapss-rul", second_artifact, {"archive": archive, "policy": policy}
    )
    compared = compare_runs(registry, str(record["run_id"]), str(added["run_id"]))
    assert compared["promotion_status"] == "research_only"
    assert len(list_runs(registry)) == 2
    third, _, _ = _registered(tmp_path / "third", seed=23)
    third_artifact = Path(str(third["artifact_dir"]))
    third_archive = Path(third["inputs"]["archive"]["path"])
    third_policy = Path(third["inputs"]["policy"]["path"])
    incompatible = register_run(
        registry,
        "cmapss-rul",
        third_artifact,
        {"archive": third_archive, "policy": third_policy},
    )
    with pytest.raises(ValueError, match="incompatible model"):
        compare_runs(registry, str(record["run_id"]), str(incompatible["run_id"]))
    different_source, _, _ = _registered(tmp_path / "different-source", truth_shift=5)
    different_artifact = Path(str(different_source["artifact_dir"]))
    different_archive = Path(different_source["inputs"]["archive"]["path"])
    different_policy = Path(different_source["inputs"]["policy"]["path"])
    different = register_run(
        registry,
        "cmapss-rul",
        different_artifact,
        {"archive": different_archive, "policy": different_policy},
    )
    with pytest.raises(ValueError, match="incompatible model"):
        compare_runs(registry, str(record["run_id"]), str(different["run_id"]))
    corrupt_id = "0" * 24
    (registry / "runs" / corrupt_id).mkdir()
    with pytest.raises(ValueError, match="cannot read JSON"):
        list_runs(registry)
    changed_record = registry / "runs" / str(added["run_id"]) / "record.json"
    payload = json.loads(changed_record.read_text())
    payload["signature"]["target"] = "failure probability"
    changed_record.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="record hash mismatch"):
        compare_runs(registry, str(record["run_id"]), str(added["run_id"]))
