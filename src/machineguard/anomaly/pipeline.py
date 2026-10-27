"""Train-only robust fitting and held-out descriptive baseline evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import statistics
import tempfile
from array import array
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from machineguard import __version__
from machineguard.anomaly.statistical import (
    AlertPersistence,
    BaselinePolicy,
    RobustStat,
    Score,
    score_physical,
    score_robust,
)
from machineguard.core.provenance import sha256_file
from machineguard.features.eda import ReportWindow, load_report_windows
from machineguard.features.pipeline import verify_features

_COLUMNS = (
    "asset_id",
    "source_row_index",
    "event_time_source",
    "split",
    "operating_load_flag",
    "tp3_mean_5m",
    "motor_current_mean_5m",
    "oil_temperature_mean_60m",
)
_SPLITS = ("validation", "test")
_BASELINES = ("physical", "robust_mad")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _rows(version_dir: Path, manifest: dict[str, Any], batch_size: int) -> Iterator[dict[str, Any]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    for part in manifest["parts"]:
        parquet = pq.ParquetFile(version_dir / part["path"])
        try:
            for batch in parquet.iter_batches(batch_size=batch_size, columns=list(_COLUMNS)):
                yield from batch.to_pylist()
        finally:
            parquet.close()


def _outside_padding(timestamp: datetime, windows: tuple[ReportWindow, ...], seconds: int) -> bool:
    margin = timedelta(seconds=seconds)
    return all(
        not window.start - margin <= timestamp < window.end_exclusive + margin for window in windows
    )


def _eligible_for_fit(
    row: dict[str, Any], windows: tuple[ReportWindow, ...], policy: BaselinePolicy
) -> bool:
    return (
        row["split"] == "train"
        and row["operating_load_flag"] in (0, 1)
        and _outside_padding(row["event_time_source"], windows, policy.report_exclusion_seconds)
    )


def fit_robust(
    version_dir: Path,
    manifest: dict[str, Any],
    policy: BaselinePolicy,
    windows: tuple[ReportWindow, ...],
    batch_size: int = 50_000,
) -> tuple[dict[str, dict[str, RobustStat]], dict[str, Any]]:
    """Two streaming passes over training rows; exact medians/MADs held in compact arrays."""
    values = {state: {name: array("d") for name in policy.signals} for state in ("0", "1")}
    fit_rows = {"0": 0, "1": 0}
    excluded_train_rows = 0
    for row in _rows(version_dir, manifest, batch_size):
        if row["split"] != "train":
            continue
        if not _eligible_for_fit(row, windows, policy):
            excluded_train_rows += 1
            continue
        state = str(row["operating_load_flag"])
        fit_rows[state] += 1
        for name in policy.signals:
            value = row[name]
            if value is not None and math.isfinite(value):
                values[state][name].append(value)
    medians: dict[str, dict[str, float]] = {"0": {}, "1": {}}
    counts: dict[str, dict[str, int]] = {"0": {}, "1": {}}
    for state in ("0", "1"):
        for name in policy.signals:
            series = values[state][name]
            counts[state][name] = len(series)
            if len(series) >= policy.min_group_samples:
                medians[state][name] = float(statistics.median(series))
            del series[:]
    deviations = {state: {name: array("d") for name in policy.signals} for state in ("0", "1")}
    for row in _rows(version_dir, manifest, batch_size):
        if not _eligible_for_fit(row, windows, policy):
            continue
        state = str(row["operating_load_flag"])
        for name, median in medians[state].items():
            value = row[name]
            if value is not None and math.isfinite(value):
                deviations[state][name].append(abs(value - median))
    stats: dict[str, dict[str, RobustStat]] = {"0": {}, "1": {}}
    unsupported: list[str] = []
    for state in ("0", "1"):
        for name in policy.signals:
            if name not in medians[state]:
                unsupported.append(f"{state}/{name}:insufficient_train_samples")
                continue
            mad = float(statistics.median(deviations[state][name]))
            fitted = RobustStat(medians[state][name], mad, counts[state][name])
            if not fitted.supported:
                unsupported.append(f"{state}/{name}:zero_mad")
            stats[state][name] = fitted
    fit_summary = {
        "fit_rows_by_state": fit_rows,
        "excluded_train_rows": excluded_train_rows,
        "non_null_samples_by_state_signal": counts,
        "unsupported": unsupported,
        "fit_split": "train",
        "report_padding_seconds": policy.report_exclusion_seconds,
    }
    return stats, fit_summary


class _Evaluation:
    def __init__(self, policy: BaselinePolicy, windows: tuple[ReportWindow, ...]) -> None:
        self.policy = policy
        self.windows = windows
        self.trackers = {name: AlertPersistence(policy) for name in _BASELINES}
        self.metrics: dict[str, dict[str, dict[str, Any]]] = {
            baseline: {
                split: {
                    "rows": 0,
                    "eligible_rows": 0,
                    "unscorable_rows": 0,
                    "raw_alert_rows": 0,
                    "triggered_signal_rows": {name: 0 for name in policy.signals},
                    "active_alert_rows": 0,
                    "active_alert_rows_by_state": {"0": 0, "1": 0},
                    "episodes": 0,
                    "off_report_episode_starts": 0,
                    "observed_off_report_seconds": 0,
                }
                for split in _SPLITS
            }
            for baseline in _BASELINES
        }
        self.event_metrics: dict[str, list[dict[str, Any]]] = {
            baseline: [
                {
                    "description": window.description,
                    "start": window.start.isoformat(),
                    "end_exclusive": window.end_exclusive.isoformat(),
                    "split": None,
                    "active_rows_in_window": 0,
                    "active_rows_preceding_hour": 0,
                    "first_active_in_window": None,
                }
                for window in windows
            ]
            for baseline in _BASELINES
        }
        self._prior: dict[str, tuple[datetime, str, bool] | None] = {
            baseline: None for baseline in _BASELINES
        }

    def observe(self, row: dict[str, Any], scores: dict[str, Score]) -> None:
        timestamp: datetime = row["event_time_source"]
        split: str = row["split"]
        for baseline, score in scores.items():
            active, started, _ = self.trackers[baseline].process(row, score)
            if split not in _SPLITS:
                self._prior[baseline] = None
                continue
            metric = self.metrics[baseline][split]
            metric["rows"] += 1
            metric["eligible_rows"] += int(score.eligible)
            metric["unscorable_rows"] += int(not score.eligible)
            metric["raw_alert_rows"] += int(score.raw_alert)
            metric["active_alert_rows"] += int(active)
            if active:
                metric["active_alert_rows_by_state"][str(row["operating_load_flag"])] += 1
            for name in score.triggered_signals:
                metric["triggered_signal_rows"][name] += 1
            metric["episodes"] += int(started)
            off_report = _outside_padding(
                timestamp, self.windows, self.policy.report_exclusion_seconds
            )
            if started and off_report:
                metric["off_report_episode_starts"] += 1
            prior = self._prior[baseline]
            if (
                score.eligible
                and off_report
                and prior is not None
                and prior[1] == split
                and prior[2]
                and 0 < (timestamp - prior[0]).total_seconds() <= self.policy.max_contiguous_seconds
            ):
                metric["observed_off_report_seconds"] += int((timestamp - prior[0]).total_seconds())
            self._prior[baseline] = (timestamp, split, score.eligible and off_report)
            for index, window in enumerate(self.windows):
                event = self.event_metrics[baseline][index]
                if window.start <= timestamp < window.end_exclusive:
                    event["split"] = split
                    if active:
                        event["active_rows_in_window"] += 1
                        if event["first_active_in_window"] is None:
                            event["first_active_in_window"] = timestamp.isoformat()
                elif window.start - timedelta(hours=1) <= timestamp < window.start and active:
                    event["active_rows_preceding_hour"] += 1

    def result(self) -> dict[str, Any]:
        for baseline in _BASELINES:
            for split in _SPLITS:
                metric = self.metrics[baseline][split]
                seconds = metric["observed_off_report_seconds"]
                metric["off_report_episodes_per_observed_day"] = (
                    metric["off_report_episode_starts"] * 86400 / seconds if seconds else None
                )
            for event in self.event_metrics[baseline]:
                event["any_active_in_window"] = event["active_rows_in_window"] > 0
        return {
            "metrics": self.metrics,
            "reported_windows": self.event_metrics,
            "metric_caveat": (
                "Off-report episodes per observed day is a false-alarm proxy, not a measured "
                "false-positive rate: unreported faults may exist. Report-window coincidence "
                "is not event recall because onset and labels are uncertain."
            ),
            "denominator_policy": (
                "Sum of adjacent eligible observed intervals <= max_contiguous_seconds, "
                "within each split and outside report windows +/- exclusion padding."
            ),
        }


def _stats_json(
    stats: dict[str, dict[str, RobustStat]],
) -> dict[str, dict[str, dict[str, float | int]]]:
    return {
        state: {
            name: {"median": value.median, "mad": value.mad, "count": value.count}
            for name, value in group.items()
        }
        for state, group in stats.items()
    }


def verify_baselines(version_dir: Path) -> dict[str, Any]:
    try:
        manifest: dict[str, Any] = json.loads((version_dir / "manifest.json").read_text())
        if manifest["version_id"] != version_dir.name:
            raise ValueError("baseline version ID mismatch")
        for filename in ("baseline.json", "evaluation.json"):
            if sha256_file(version_dir / filename) != manifest[filename + "_sha256"]:
                raise ValueError(f"{filename} hash mismatch")
        return manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid baseline artifact: {exc}") from exc


def run_baselines(
    feature_version: Path,
    output_root: Path,
    policy_path: Path,
    windows_path: Path,
    *,
    batch_size: int = 50_000,
) -> Path:
    """Fit on train only, score validation/test once, and publish local verified artifacts."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    features = verify_features(feature_version)
    policy = BaselinePolicy.from_json_file(policy_path)
    windows = load_report_windows(windows_path)
    if policy.feature_version_id != features["version_id"]:
        raise ValueError("policy is bound to a different feature version")
    if sha256_file(windows_path) != features["identity"]["report_windows_sha256"]:
        raise ValueError("report windows differ from feature-version evidence")
    if features["split_plan"]["usage"] != "anomaly_descriptive_only":
        raise ValueError("feature split is not approved for anomaly work")
    identity = {
        "feature_manifest_sha256": sha256_file(feature_version / "manifest.json"),
        "policy_sha256": sha256_file(policy_path),
        "report_windows_sha256": sha256_file(windows_path),
        "builder_version": __version__,
        "builder_code_sha256": hashlib.sha256(
            "".join(
                sha256_file(Path(__file__).with_name(name))
                for name in ("statistical.py", "pipeline.py")
            ).encode()
        ).hexdigest(),
    }
    version_id = hashlib.sha256(_json(identity).encode()).hexdigest()[:24]
    root = output_root / "metropt-3-uci" / version_id
    if root.exists():
        existing = verify_baselines(root)
        if existing["identity"] != identity:
            raise ValueError("baseline identity mismatch")
        return root
    stats, fit_summary = fit_robust(feature_version, features, policy, windows, batch_size)
    evaluation = _Evaluation(policy, windows)
    for row in _rows(feature_version, features, batch_size):
        if row["split"] not in _SPLITS:
            continue
        evaluation.observe(
            row,
            {
                "physical": score_physical(row, policy),
                "robust_mad": score_robust(row, policy, stats),
            },
        )
    result = evaluation.result()
    result["feature_version_id"] = features["version_id"]
    result["fit_summary"] = fit_summary
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root.parent))
    try:
        baseline = {
            "version_id": version_id,
            "feature_version_id": features["version_id"],
            "source_sha256": features["source_sha256"],
            "policy": json.loads(policy_path.read_text()),
            "robust_stats": _stats_json(stats),
            "fit_summary": fit_summary,
            "interpretation": (
                "Anomaly deviation scores and assumed physical guardrails; "
                "no failure probabilities."
            ),
        }
        (staging / "baseline.json").write_text(_json(baseline) + "\n", encoding="utf-8")
        (staging / "evaluation.json").write_text(_json(result) + "\n", encoding="utf-8")
        manifest = {
            "version_id": version_id,
            "identity": identity,
            "baseline.json_sha256": sha256_file(staging / "baseline.json"),
            "evaluation.json_sha256": sha256_file(staging / "evaluation.json"),
            "published_at_utc": datetime.now(UTC).isoformat(),
        }
        (staging / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
        if root.exists():
            verify_baselines(root)
            return root
        staging.rename(root)
        verify_baselines(root)
        return root
    finally:
        if staging.exists():
            shutil.rmtree(staging)
