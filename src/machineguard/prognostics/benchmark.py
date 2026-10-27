"""Fixed, engine-isolated C-MAPSS RUL benchmark; artifacts remain local."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from zipfile import BadZipFile, ZipFile

import numpy as np
import sklearn  # type: ignore[import-untyped]
from numpy.typing import NDArray
from sklearn.linear_model import Ridge  # type: ignore[import-untyped]
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]

from machineguard import __version__
from machineguard.core.provenance import sha256_file
from machineguard.prognostics.cmapss import (
    FEATURE_NAMES,
    SUBSETS,
    Trajectory,
    read_member,
    read_terminal_truth,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True, slots=True)
class BenchmarkPolicy:
    dataset_id: str
    source_url: str
    archive_sha256: str
    subset_counts: dict[str, dict[str, int]]
    seed: int
    validation_fraction: float
    validation_cutoff_fraction_min: float
    validation_cutoff_fraction_max: float
    ridge_alpha: float
    target_policy: str
    test_policy: str

    @classmethod
    def from_file(cls, path: Path) -> BenchmarkPolicy:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("C-MAPSS benchmark policy fields do not match schema")
        if (
            data["dataset_id"] != "nasa-cmapss-classic"
            or not isinstance(data["source_url"], str)
            or not data["source_url"].startswith("https://")
            or not isinstance(data["archive_sha256"], str)
            or len(data["archive_sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in data["archive_sha256"])
            or type(data["seed"]) is not int
            or data["target_policy"]
            != "uncapped remaining operational cycles; no conversion to wall-clock time"
            or data["test_policy"]
            != "publisher terminal truth opened only after fixed training and validation evaluation"
        ):
            raise ValueError("invalid C-MAPSS benchmark identity or policy")
        if (
            type(data["validation_fraction"]) not in (float, int)
            or not 0 < data["validation_fraction"] < 0.5
            or type(data["validation_cutoff_fraction_min"]) not in (float, int)
            or type(data["validation_cutoff_fraction_max"]) not in (float, int)
            or not 0
            < data["validation_cutoff_fraction_min"]
            < data["validation_cutoff_fraction_max"]
            < 1
            or type(data["ridge_alpha"]) not in (float, int)
            or not math.isfinite(data["ridge_alpha"])
            or data["ridge_alpha"] <= 0
        ):
            raise ValueError("invalid validation or Ridge settings")
        counts = data["subset_counts"]
        if not isinstance(counts, dict) or set(counts) != set(SUBSETS):
            raise ValueError("all four subset counts are required")
        for subset in SUBSETS:
            item = counts[subset]
            if (
                not isinstance(item, dict)
                or set(item) != {"train_rows", "train_engines", "test_rows", "test_engines"}
                or any(type(value) is not int or value <= 0 for value in item.values())
            ):
                raise ValueError(f"invalid expected counts for {subset}")
        return cls(**data)


def _hash_number(seed: int, subset: str, unit_id: int, purpose: str) -> int:
    payload = f"{seed}:{subset}:{unit_id}:{purpose}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def engine_group_split(
    engines: tuple[Trajectory, ...], seed: int, subset: str, fraction: float
) -> tuple[tuple[Trajectory, ...], tuple[Trajectory, ...]]:
    """Select whole validation engines using stable hashes, never row splits."""
    if len(engines) < 5 or not 0 < fraction < 0.5:
        raise ValueError("at least five engines and a valid fraction are required")
    ids = {engine.unit_id for engine in engines}
    if len(ids) != len(engines):
        raise ValueError("duplicate engine ID")
    ordered = sorted(engines, key=lambda item: _hash_number(seed, subset, item.unit_id, "fold"))
    n_validation = max(1, round(len(engines) * fraction))
    validation_ids = {item.unit_id for item in ordered[:n_validation]}
    return (
        tuple(item for item in engines if item.unit_id not in validation_ids),
        tuple(item for item in engines if item.unit_id in validation_ids),
    )


def validation_cutoff(engine: Trajectory, policy: BenchmarkPolicy, subset: str) -> int:
    """Choose a reproducible observed prefix without exposing later cycles to predictors."""
    if engine.lifetime_observed < 2:
        raise ValueError("validation engine requires at least two cycles")
    fraction = _hash_number(policy.seed, subset, engine.unit_id, "cutoff") / 2**64
    fraction = policy.validation_cutoff_fraction_min + fraction * (
        policy.validation_cutoff_fraction_max - policy.validation_cutoff_fraction_min
    )
    return min(engine.lifetime_observed - 1, max(1, int(engine.lifetime_observed * fraction)))


def _training_arrays(
    engines: tuple[Trajectory, ...],
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    x = np.vstack([engine.features for engine in engines])
    y = np.concatenate([engine.lifetime_observed - engine.features[:, 0] for engine in engines])
    weights = np.concatenate(
        [np.full(len(engine.features), 1 / len(engine.features)) for engine in engines]
    )
    return x, y, weights


def _metrics(truth: NDArray[np.float64], prediction: NDArray[np.float64]) -> dict[str, Any]:
    if truth.shape != prediction.shape or truth.ndim != 1 or not len(truth):
        raise ValueError("metric inputs must be nonempty, aligned one-dimensional arrays")
    error = prediction - truth
    result: dict[str, Any] = {
        "engines": len(truth),
        "mae_cycles": round(float(np.mean(np.abs(error))), 6),
        "rmse_cycles": round(float(np.sqrt(np.mean(error**2))), 6),
        "bias_cycles": round(float(np.mean(error)), 6),
    }
    slices: dict[str, Any] = {}
    for name, mask in (
        ("rul_0_50", truth <= 50),
        ("rul_51_125", (truth > 50) & (truth <= 125)),
        ("rul_over_125", truth > 125),
    ):
        slices[name] = _metrics_basic(truth[mask], prediction[mask]) if mask.any() else None
    result["by_true_rul"] = slices
    return result


def _metrics_basic(truth: NDArray[np.float64], prediction: NDArray[np.float64]) -> dict[str, Any]:
    error = prediction - truth
    return {
        "engines": len(truth),
        "mae_cycles": round(float(np.mean(np.abs(error))), 6),
        "rmse_cycles": round(float(np.sqrt(np.mean(error**2))), 6),
    }


def _validate_counts(
    subset: str, kind: str, engines: tuple[Trajectory, ...], rows: int, expected: dict[str, int]
) -> None:
    if len(engines) != expected[f"{kind}_engines"] or rows != expected[f"{kind}_rows"]:
        raise ValueError(f"{subset} {kind} row/engine count differs from source policy")


def _evaluate_subset(zip_file: ZipFile, subset: str, policy: BenchmarkPolicy) -> dict[str, Any]:
    expected = policy.subset_counts[subset]
    train, train_hash, train_rows = read_member(zip_file, f"train_{subset}.txt")
    _validate_counts(subset, "train", train, train_rows, expected)
    fit, validation = engine_group_split(train, policy.seed, subset, policy.validation_fraction)
    x_fit, y_fit, weights = _training_arrays(fit)
    scaler = StandardScaler().fit(x_fit)
    ridge = Ridge(alpha=policy.ridge_alpha).fit(
        scaler.transform(x_fit), y_fit, sample_weight=weights
    )
    median_lifetime = float(np.median([engine.lifetime_observed for engine in fit]))
    cutoffs = {engine.unit_id: validation_cutoff(engine, policy, subset) for engine in validation}
    x_validation = np.vstack(
        [engine.features[cutoffs[engine.unit_id] - 1] for engine in validation]
    )
    y_validation = np.asarray(
        [engine.lifetime_observed - cutoffs[engine.unit_id] for engine in validation],
        dtype=np.float64,
    )
    baseline_validation = np.maximum(0, median_lifetime - x_validation[:, 0])
    ridge_validation = np.maximum(0, ridge.predict(scaler.transform(x_validation)))
    validation_metrics = {
        "median_lifetime_baseline": _metrics(y_validation, baseline_validation),
        "ridge": _metrics(y_validation, ridge_validation),
    }

    # Publisher test truth is opened only after fit and validation evaluation are fixed.
    test, test_hash, test_rows = read_member(zip_file, f"test_{subset}.txt")
    _validate_counts(subset, "test", test, test_rows, expected)
    with zip_file.open(f"RUL_{subset}.txt") as truth_stream:
        truth, truth_hash = read_terminal_truth(truth_stream, len(test))
    x_test = np.vstack([engine.features[-1] for engine in test])
    y_test = truth.astype(np.float64)
    baseline_test = np.maximum(0, median_lifetime - x_test[:, 0])
    ridge_test = np.maximum(0, ridge.predict(scaler.transform(x_test)))
    coefficient_hash = hashlib.sha256(np.asarray(ridge.coef_, dtype="<f8").tobytes()).hexdigest()
    return {
        "subset": subset,
        "member_sha256": {"train": train_hash, "test": test_hash, "truth": truth_hash},
        "observed_counts": {
            "train_rows": train_rows,
            "train_engines": len(train),
            "test_rows": test_rows,
            "test_engines": len(test),
            "truth_values": len(truth),
        },
        "folds": {
            "fit_engine_ids": [item.unit_id for item in fit],
            "validation_engine_ids": [item.unit_id for item in validation],
            "publisher_test_engine_ids": [item.unit_id for item in test],
            "publisher_test_id_namespace": f"{subset}:test",
        },
        "validation_cutoffs": cutoffs,
        "fit_rows": len(x_fit),
        "median_fit_lifetime_cycles": median_lifetime,
        "ridge_coefficient_sha256": coefficient_hash,
        "validation": validation_metrics,
        "publisher_test": {
            "median_lifetime_baseline": _metrics(y_test, baseline_test),
            "ridge": _metrics(y_test, ridge_test),
        },
    }


def verify_benchmark(version_dir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
        content = (version_dir / "report.json").read_bytes()
        if manifest["version_id"] != version_dir.name:
            raise ValueError("benchmark version ID mismatch")
        if hashlib.sha256(content).hexdigest() != manifest["report_sha256"]:
            raise ValueError("benchmark report hash mismatch")
        report = json.loads(content)
        if hashlib.sha256(_canonical(report["identity"])).hexdigest()[:24] != version_dir.name:
            raise ValueError("benchmark identity digest mismatch")
        if report["archive_sha256"] != manifest["archive_sha256"]:
            raise ValueError("benchmark archive identity mismatch")
        return cast(dict[str, Any], report)
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid C-MAPSS benchmark artifact: {exc}") from exc


def run_benchmark(archive: Path, output_root: Path, policy_path: Path) -> Path:
    policy = BenchmarkPolicy.from_file(policy_path)
    archive_sha = sha256_file(archive)
    if archive_sha != policy.archive_sha256:
        raise ValueError("C-MAPSS archive SHA-256 differs from policy")
    identity = {
        "archive_sha256": archive_sha,
        "policy_sha256": sha256_file(policy_path),
        "code_sha256": sha256_file(Path(__file__)),
        "package_version": __version__,
        "numpy_version": np.__version__,
        "sklearn_version": sklearn.__version__,
    }
    version_id = hashlib.sha256(_canonical(identity)).hexdigest()[:24]
    version_dir = output_root / "nasa-cmapss-classic" / version_id
    if version_dir.exists():
        report = verify_benchmark(version_dir)
        if report["identity"] != identity:
            raise ValueError("existing benchmark version differs from requested inputs")
        return version_dir
    try:
        with ZipFile(archive) as zip_file:
            required = {
                f"{kind}_{subset}.txt" for subset in SUBSETS for kind in ("train", "test", "RUL")
            }
            if not required.issubset(set(zip_file.namelist())):
                raise ValueError("C-MAPSS archive lacks required subset members")
            results = {subset: _evaluate_subset(zip_file, subset, policy) for subset in SUBSETS}
    except BadZipFile as exc:
        raise ValueError("invalid C-MAPSS ZIP archive") from exc
    report = {
        "identity": identity,
        "archive_sha256": archive_sha,
        "source_url": policy.source_url,
        "target": "uncapped remaining operational cycles at observed last cycle",
        "domain": "simulated aircraft turbofans; no industrial compressor validation",
        "protocol": {
            "seed": policy.seed,
            "validation_fraction": policy.validation_fraction,
            "cutoff_fraction_range": [
                policy.validation_cutoff_fraction_min,
                policy.validation_cutoff_fraction_max,
            ],
            "ridge_alpha": policy.ridge_alpha,
            "feature_names": FEATURE_NAMES,
            "train_weighting": "each fit engine has equal total row weight",
            "baseline": "nonnegative median fit-engine lifetime minus observed cycle",
            "regression": "train-only StandardScaler plus Ridge, clipped at zero",
            "test_truth_access": policy.test_policy,
        },
        "subsets": results,
    }
    content = _canonical(report) + b"\n"
    manifest = {
        "version_id": version_id,
        "archive_sha256": archive_sha,
        "report_sha256": hashlib.sha256(content).hexdigest(),
    }
    version_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cmapss-", dir=version_dir.parent) as temp:
        staging = Path(temp) / version_id
        staging.mkdir()
        (staging / "report.json").write_bytes(content)
        (staging / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
        if version_dir.exists():
            verify_benchmark(version_dir)
        else:
            os.replace(staging, version_dir)
    return version_dir
