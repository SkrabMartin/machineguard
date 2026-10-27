"""Reproducible state-conditional Isolation Forest experiment and comparison."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import shutil
import tempfile
from array import array
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from machineguard import __version__
from machineguard.anomaly.models import SIGNALS, ModelPolicy, TrainedState, fit_state
from machineguard.anomaly.pipeline import verify_baselines
from machineguard.anomaly.statistical import BaselinePolicy
from machineguard.core.provenance import sha256_file
from machineguard.evaluation.events import EventEvaluation, outside_report_padding
from machineguard.features.eda import ReportWindow, load_report_windows
from machineguard.features.pipeline import verify_features

_COLUMNS = (
    "asset_id",
    "source_row_index",
    "event_time_source",
    "split",
    "operating_load_flag",
    *SIGNALS,
)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _iter_batches(
    feature_dir: Path, manifest: dict[str, Any], batch_size: int
) -> Iterator[list[dict[str, Any]]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    for part in manifest["parts"]:
        parquet = pq.ParquetFile(feature_dir / part["path"])
        try:
            for batch in parquet.iter_batches(batch_size=batch_size, columns=list(_COLUMNS)):
                yield batch.to_pylist()
        finally:
            parquet.close()


def _vector(row: dict[str, Any]) -> list[float] | None:
    values = [row[name] for name in SIGNALS]
    if any(value is None or not math.isfinite(value) for value in values):
        return None
    return [float(value) for value in values]


def _fit_models(
    feature_dir: Path,
    manifest: dict[str, Any],
    policy: ModelPolicy,
    baseline_policy: BaselinePolicy,
    windows: tuple[ReportWindow, ...],
    batch_size: int,
) -> tuple[dict[str, TrainedState], dict[str, Any]]:
    data = {state: array("d") for state in ("0", "1")}
    counts = {"0": 0, "1": 0}
    excluded_by_report = 0
    excluded_missing = 0
    for batch in _iter_batches(feature_dir, manifest, batch_size):
        for row in batch:
            if row["split"] != "train" or row["operating_load_flag"] not in (0, 1):
                continue
            if not outside_report_padding(
                row["event_time_source"], windows, baseline_policy.report_exclusion_seconds
            ):
                excluded_by_report += 1
                continue
            vector = _vector(row)
            if vector is None:
                excluded_missing += 1
                continue
            state = str(row["operating_load_flag"])
            data[state].extend(vector)
            counts[state] += 1
    fitted: dict[str, TrainedState] = {}
    unsupported: list[str] = []
    for state in ("0", "1"):
        if counts[state] < policy.min_group_samples:
            unsupported.append(f"{state}:insufficient_complete_train_rows")
            continue
        matrix = np.frombuffer(data[state], dtype=np.float64).reshape(-1, len(SIGNALS))
        fitted[state] = fit_state(matrix, policy)
    return fitted, {
        "complete_train_rows_by_state": counts,
        "excluded_train_rows_report_padding": excluded_by_report,
        "excluded_train_rows_missing_signals": excluded_missing,
        "unsupported_states": unsupported,
        "fit_split": "train",
    }


class _DiagnosticSample:
    """Keep a bounded, stable hash sample of newly activated alert episodes."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.samples: dict[str, list[tuple[int, int, str, list[float]]]] = {
            "validation": [],
            "test": [],
        }

    def consider(self, row: dict[str, Any], vector: list[float]) -> None:
        split = row["split"]
        if split not in self.samples:
            return
        index = int(row["source_row_index"])
        digest = hashlib.sha256(f"{row['asset_id']}:{index}".encode()).digest()
        rank = int.from_bytes(digest[:8], "big")
        entry = (-rank, index, str(row["operating_load_flag"]), vector)
        heap = self.samples[split]
        if len(heap) < self.limit:
            heapq.heappush(heap, entry)
        elif rank < -heap[0][0]:
            heapq.heapreplace(heap, entry)

    def summarize(self, models: dict[str, TrainedState]) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for split, heap in self.samples.items():
            positive_sums = {name: 0.0 for name in SIGNALS}
            top_counts = {name: 0 for name in SIGNALS}
            for _, _, state, vector in heap:
                delta = models[state].diagnostic_deltas(np.asarray(vector, dtype=np.float64))
                for name, value in zip(SIGNALS, delta, strict=True):
                    positive_sums[name] += max(0.0, float(value))
                top_index = int(np.argmax(delta))
                if delta[top_index] > 0:
                    top_counts[SIGNALS[top_index]] += 1
            n = len(heap)
            report[split] = {
                "sampled_episodes": n,
                "mean_positive_decision_delta_by_signal": {
                    name: value / n if n else None for name, value in positive_sums.items()
                },
                "top_positive_signal_counts": top_counts,
            }
        return {
            "method": (
                "For a deterministic hash sample of episode starts, replace one feature "
                "with its training-state median and measure the decision-score increase. "
                "Positive deltas are associations, not causal attributions; correlated "
                "features and sample selection limit interpretation."
            ),
            "by_split": report,
        }


def _score_experiment(
    feature_dir: Path,
    manifest: dict[str, Any],
    models: dict[str, TrainedState],
    policy: ModelPolicy,
    baseline_policy: BaselinePolicy,
    windows: tuple[ReportWindow, ...],
    batch_size: int,
) -> dict[str, Any]:
    evaluator = EventEvaluation(baseline_policy, windows)
    diagnostics = _DiagnosticSample(policy.diagnostic_episode_sample_size)
    for batch in _iter_batches(feature_dir, manifest, batch_size):
        decisions: list[float | None] = [None] * len(batch)
        vectors: dict[int, list[float]] = {}
        by_state: dict[str, list[int]] = {"0": [], "1": []}
        for index, row in enumerate(batch):
            if row["split"] not in ("validation", "test"):
                continue
            state = str(row["operating_load_flag"])
            if state not in models:
                continue
            vector = _vector(row)
            if vector is None:
                continue
            vectors[index] = vector
            by_state[state].append(index)
        for state, indices in by_state.items():
            if not indices:
                continue
            matrix = np.asarray([vectors[index] for index in indices], dtype=np.float64)
            scores = models[state].decision_function(matrix)
            for index, value in zip(indices, scores, strict=True):
                decisions[index] = float(value)
        for index, row in enumerate(batch):
            if row["split"] not in ("validation", "test"):
                continue
            started = evaluator.observe(row, decisions[index])
            if started and index in vectors:
                diagnostics.consider(row, vectors[index])
    result = evaluator.result()
    result["diagnostics"] = diagnostics.summarize(models)
    return result


def _compare(model_eval: dict[str, Any], baseline_eval: dict[str, Any]) -> dict[str, Any]:
    comparison: dict[str, Any] = {}
    for split in ("validation", "test"):
        comparison[split] = {}
        for name in ("physical", "robust_mad"):
            metric = baseline_eval["metrics"][name][split]
            comparison[split][name] = {
                "eligible_rows": metric["eligible_rows"],
                "episodes": metric["episodes"],
                "off_report_episodes_per_observed_day": metric[
                    "off_report_episodes_per_observed_day"
                ],
            }
        metric = model_eval["metrics"][split]
        comparison[split]["isolation_forest"] = {
            "eligible_rows": metric["eligible_rows"],
            "episodes": metric["episodes"],
            "off_report_episodes_per_observed_day": metric["off_report_episodes_per_observed_day"],
        }
    for name in ("physical", "robust_mad"):
        comparison[name + "_report_window_coincidence"] = [
            event["any_active_in_window"] for event in baseline_eval["reported_windows"][name]
        ]
    comparison["isolation_forest_report_window_coincidence"] = [
        event["any_active_inclusive_minute"] for event in model_eval["reported_windows"]
    ]
    return comparison


def _verify_multivariate(version_dir: Path, expected_version_id: str) -> dict[str, Any]:
    try:
        manifest: dict[str, Any] = json.loads((version_dir / "manifest.json").read_text())
        if manifest["version_id"] != expected_version_id:
            raise ValueError("multivariate version ID mismatch")
        for filename in ("model_metadata.json", "evaluation.json"):
            if sha256_file(version_dir / filename) != manifest[filename + "_sha256"]:
                raise ValueError(f"{filename} hash mismatch")
        return manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid multivariate artifact: {exc}") from exc


def verify_multivariate(version_dir: Path) -> dict[str, Any]:
    return _verify_multivariate(version_dir, version_dir.name)


def run_multivariate(
    feature_dir: Path,
    baseline_dir: Path,
    output_root: Path,
    policy_path: Path,
    baseline_policy_path: Path,
    windows_path: Path,
    *,
    batch_size: int = 20_000,
) -> Path:
    """Refit deterministically from verified inputs; never deserialize a model object."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    feature_manifest = verify_features(feature_dir)
    baseline_manifest = verify_baselines(baseline_dir)
    policy = ModelPolicy.from_json_file(policy_path)
    baseline_policy = BaselinePolicy.from_json_file(baseline_policy_path)
    windows = load_report_windows(windows_path)
    if policy.feature_version_id != feature_manifest["version_id"]:
        raise ValueError("model policy feature version mismatch")
    if policy.baseline_version_id != baseline_manifest["version_id"]:
        raise ValueError("model policy baseline version mismatch")
    if baseline_policy.feature_version_id != feature_manifest["version_id"]:
        raise ValueError("baseline policy feature version mismatch")
    if baseline_manifest["identity"]["policy_sha256"] != sha256_file(baseline_policy_path):
        raise ValueError("baseline policy hash mismatch")
    if any(
        identity["report_windows_sha256"] != sha256_file(windows_path)
        for identity in (feature_manifest["identity"], baseline_manifest["identity"])
    ):
        raise ValueError("report windows differ from verified feature/baseline evidence")
    baseline_data = json.loads((baseline_dir / "baseline.json").read_text())
    baseline_eval = json.loads((baseline_dir / "evaluation.json").read_text())
    if baseline_data["feature_version_id"] != feature_manifest["version_id"]:
        raise ValueError("baseline artifact belongs to a different feature version")
    identity = {
        "feature_manifest_sha256": sha256_file(feature_dir / "manifest.json"),
        "baseline_manifest_sha256": sha256_file(baseline_dir / "manifest.json"),
        "model_policy_sha256": sha256_file(policy_path),
        "baseline_policy_sha256": sha256_file(baseline_policy_path),
        "report_windows_sha256": sha256_file(windows_path),
        "builder_version": __version__,
        "numpy_version": np.__version__,
        "sklearn_version": version("scikit-learn"),
        "scipy_version": version("scipy"),
        "builder_code_sha256": hashlib.sha256(
            "".join(
                sha256_file(path)
                for path in (
                    Path(__file__),
                    Path(__file__).with_name("models.py"),
                    Path(__file__).parents[1] / "evaluation" / "events.py",
                )
            ).encode()
        ).hexdigest(),
    }
    version_id = hashlib.sha256(_json(identity).encode()).hexdigest()[:24]
    final_dir = output_root / "metropt-3-uci" / version_id
    if final_dir.exists():
        existing = verify_multivariate(final_dir)
        if existing["identity"] != identity:
            raise ValueError("multivariate identity mismatch")
        return final_dir
    models, fit_summary = _fit_models(
        feature_dir, feature_manifest, policy, baseline_policy, windows, batch_size
    )
    if len(models) != 2:
        raise ValueError("both observed operating states require train-fitted models")
    evaluation = _score_experiment(
        feature_dir, feature_manifest, models, policy, baseline_policy, windows, batch_size
    )
    evaluation["comparison"] = _compare(evaluation, baseline_eval)
    evaluation["fit_summary"] = fit_summary
    evaluation["interpretation"] = (
        "Descriptive anomaly comparison only; report windows are not row-level labels, "
        "off-report alerts are not verified false positives, and scores are not probabilities."
    )
    metadata = {
        "version_id": version_id,
        "feature_version_id": feature_manifest["version_id"],
        "baseline_version_id": baseline_manifest["version_id"],
        "policy": json.loads(policy_path.read_text(encoding="utf-8")),
        "sklearn_version": version("scikit-learn"),
        "numpy_version": np.__version__,
        "scipy_version": version("scipy"),
        "fit_summary": fit_summary,
        "states": {name: model.metadata() for name, model in models.items()},
        "serialization_policy": (
            "No executable model object is serialized. Refit from verified feature version "
            "and locked dependencies to reproduce scores."
        ),
    }
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=final_dir.parent))
    try:
        (staging / "model_metadata.json").write_text(_json(metadata) + "\n", encoding="utf-8")
        (staging / "evaluation.json").write_text(_json(evaluation) + "\n", encoding="utf-8")
        manifest = {
            "version_id": version_id,
            "identity": identity,
            "model_metadata.json_sha256": sha256_file(staging / "model_metadata.json"),
            "evaluation.json_sha256": sha256_file(staging / "evaluation.json"),
            "published_at_utc": datetime.now(UTC).isoformat(),
        }
        (staging / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
        _verify_multivariate(staging, version_id)
        if final_dir.exists():
            verify_multivariate(final_dir)
            return final_dir
        staging.rename(final_dir)
        verify_multivariate(final_dir)
        return final_dir
    finally:
        if staging.exists():
            shutil.rmtree(staging)
