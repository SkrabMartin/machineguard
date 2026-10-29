"""Checkpointed replay of verified MetroPT canonical data to local JSONL traces."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from machineguard import __version__
from machineguard.anomaly.pipeline import verify_baselines
from machineguard.anomaly.statistical import (
    AlertPersistence,
    BaselinePolicy,
    RobustStat,
    Score,
    score_physical,
    score_robust,
)
from machineguard.core.provenance import sha256_file
from machineguard.features.engine import FeatureInput
from machineguard.features.pipeline import verify_features
from machineguard.features.spec import FeatureSpec
from machineguard.features.splits import SplitPlan
from machineguard.replay.engine import ReplayEngine, ReplayPolicy
from machineguard.storage.parquet import iter_canonical_batches, verify_dataset

_CHAIN_START = "0" * 64


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object at {path}")
    return value


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=".replay-", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(_canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


class _Trace:
    def __init__(self, path: Path, state: dict[str, Any] | None) -> None:
        self.path = path
        if state is None:
            if path.exists():
                raise ValueError(f"uncheckpointed trace already exists: {path}")
            self.bytes = 0
            self.chain = _CHAIN_START
            self.stream = path.open("w+b")
        else:
            self.bytes = int(state["bytes"])
            self.chain = state["chain_sha256"]
            if not path.is_file() or path.stat().st_size < self.bytes:
                raise ValueError(f"committed trace bytes unavailable: {path}")
            self.stream = path.open("r+b")
            observed = _CHAIN_START
            remaining = self.bytes
            while remaining:
                line = self.stream.readline(remaining)
                if not line or not line.endswith(b"\n"):
                    raise ValueError(f"truncated committed trace line: {path}")
                observed = hashlib.sha256(bytes.fromhex(observed) + line).hexdigest()
                remaining -= len(line)
            if observed != self.chain:
                raise ValueError(f"committed trace hash mismatch: {path}")
            self.stream.truncate(self.bytes)
            self.stream.seek(self.bytes)

    def append(self, value: dict[str, Any]) -> None:
        line = _canonical(value) + b"\n"
        self.stream.write(line)
        self.bytes += len(line)
        self.chain = hashlib.sha256(bytes.fromhex(self.chain) + line).hexdigest()

    def sync(self) -> dict[str, Any]:
        self.stream.flush()
        os.fsync(self.stream.fileno())
        return {"bytes": self.bytes, "chain_sha256": self.chain}

    def close(self) -> None:
        self.stream.close()


def _baseline(
    baseline_dir: Path | None, policy_path: Path | None, source_sha256: str
) -> tuple[BaselinePolicy | None, dict[str, dict[str, RobustStat]] | None]:
    if baseline_dir is None and policy_path is None:
        return None, None
    if baseline_dir is None or policy_path is None:
        raise ValueError("baseline artifact and policy must be supplied together")
    manifest = verify_baselines(baseline_dir)
    if sha256_file(policy_path) != manifest["identity"]["policy_sha256"]:
        raise ValueError("baseline policy hash mismatch")
    data = _read_json(baseline_dir / "baseline.json")
    if data["source_sha256"] != source_sha256:
        raise ValueError("baseline source hash mismatch")
    policy = BaselinePolicy.from_json_file(policy_path)
    stats = {
        state: {name: RobustStat(**values) for name, values in group.items()}
        for state, group in data["robust_stats"].items()
    }
    return policy, stats


def _identity(
    canonical_dir: Path,
    spec_path: Path,
    split_path: Path,
    baseline_dir: Path | None,
    policy_path: Path | None,
    replay_policy: ReplayPolicy,
) -> dict[str, Any]:
    spec = FeatureSpec.from_json_file(spec_path)
    split = SplitPlan.from_json_file(split_path)
    return {
        "canonical_manifest_sha256": sha256_file(canonical_dir / "manifest.json"),
        "spec_sha256": sha256_file(spec_path),
        "split_sha256": sha256_file(split_path),
        "feature_spec": spec.to_dict(),
        "split_plan": split.to_dict(),
        "baseline_manifest_sha256": (
            sha256_file(baseline_dir / "manifest.json") if baseline_dir else None
        ),
        "baseline_policy_sha256": sha256_file(policy_path) if policy_path else None,
        "baseline_dir": str(baseline_dir.resolve()) if baseline_dir else None,
        "baseline_policy_path": str(policy_path.resolve()) if policy_path else None,
        "replay_policy": asdict(replay_policy),
        "feature_engine_sha256": sha256_file(Path(__file__).parents[1] / "features" / "engine.py"),
        "replay_engine_sha256": sha256_file(Path(__file__).with_name("engine.py")),
        "replay_pipeline_sha256": sha256_file(Path(__file__)),
        "package_version": __version__,
    }


def _checkpoint(path: Path, payload: dict[str, Any]) -> None:
    _write_atomic(
        path, {"payload": payload, "sha256": hashlib.sha256(_canonical(payload)).hexdigest()}
    )


def _load_checkpoint(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    payload = data["payload"]
    if hashlib.sha256(_canonical(payload)).hexdigest() != data["sha256"]:
        raise ValueError("replay checkpoint hash mismatch")
    return cast(dict[str, Any], payload)


def _iter_events(canonical_dir: Path, batch_size: int) -> Iterator[FeatureInput]:
    for batch in iter_canonical_batches(canonical_dir, batch_size=batch_size):
        for row in batch.to_pylist():
            yield FeatureInput(**{name: row[name] for name in FeatureInput.__dataclass_fields__})


def _report(
    root: Path,
    identity: dict[str, Any],
    engine: ReplayEngine,
    wall_seconds: float,
    complete: bool,
) -> dict[str, Any]:
    return {
        "identity": identity,
        "status": "complete" if complete else "partial_checkpointed",
        "arrivals": engine.arrivals,
        "decisions": engine.decisions,
        "duplicate_keys": engine.duplicates,
        "late_quarantined": engine.late,
        "sampling_gaps": engine.gaps,
        "processing_latency_ms": {
            "mean": engine.processing_ns / engine.decisions / 1e6 if engine.decisions else None,
            "max": engine.max_processing_ns / 1e6 if engine.decisions else None,
            "bucket_counts_lt_1_10_100_1000_ms_and_ge_1000_ms": engine.latency_buckets,
        },
        "wall_seconds": wall_seconds,
        "decisions_per_wall_second": engine.decisions / wall_seconds if wall_seconds > 0 else None,
        "trace_sha256": sha256_file(root / "decisions.jsonl"),
        "quality_sha256": sha256_file(root / "quality.jsonl"),
        "time_basis": "source_naive_unresolved; no UTC conversion",
        "score_interpretation": "descriptive anomaly scores only; no failure probability",
    }


def run_replay(
    canonical_dir: Path,
    output_root: Path,
    spec_path: Path,
    split_path: Path,
    replay_policy: ReplayPolicy,
    *,
    baseline_dir: Path | None = None,
    baseline_policy_path: Path | None = None,
    max_arrivals: int | None = None,
    batch_size: int = 10_000,
) -> Path:
    """Replay canonical rows with crash-safe output/checkpoint alignment.

    A finite max_arrivals intentionally leaves a resumable partial run.
    """
    if max_arrivals is not None and max_arrivals < 1:
        raise ValueError("max_arrivals must be positive")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    source = verify_dataset(canonical_dir)
    spec = FeatureSpec.from_json_file(spec_path)
    split = SplitPlan.from_json_file(split_path)
    if (
        split.dataset_version_id != source["version_id"]
        or split.source_sha256 != source["source"]["sha256"]
    ):
        raise ValueError("replay split does not match canonical source")
    baseline_policy, robust_stats = _baseline(
        baseline_dir, baseline_policy_path, source["source"]["sha256"]
    )
    identity = _identity(
        canonical_dir, spec_path, split_path, baseline_dir, baseline_policy_path, replay_policy
    )
    run_id = hashlib.sha256(_canonical(identity)).hexdigest()[:24]
    root = output_root / "metropt-replay" / run_id
    checkpoint_path = root / "checkpoint.json"
    if root.exists() and not checkpoint_path.exists():
        unexpected = set(root.iterdir()) - {root / "decisions.jsonl", root / "quality.jsonl"}
        if unexpected:
            raise ValueError("replay directory exists without a checkpoint")
        for orphan in root.iterdir():
            orphan.unlink()
    root.mkdir(parents=True, exist_ok=True)
    old = _load_checkpoint(checkpoint_path) if checkpoint_path.exists() else None
    if old is not None and old["identity"] != identity:
        raise ValueError("replay checkpoint identity mismatch")
    if old is not None and old["complete"] and (root / "report.json").exists():
        verify_replay_report(root)
        return root
    engine = (
        ReplayEngine.from_snapshot(
            spec, split, replay_policy, old["engine"], baseline_policy, robust_stats
        )
        if old is not None
        else ReplayEngine(spec, split, replay_policy, baseline_policy, robust_stats)
    )
    traces = old["traces"] if old else {}
    decisions = _Trace(root / "decisions.jsonl", traces.get("decisions"))
    quality = _Trace(root / "quality.jsonl", traces.get("quality"))
    prior_wall = float(old["wall_seconds"]) if old else 0.0
    started = time.monotonic()

    def save(complete: bool) -> None:
        payload = {
            "identity": identity,
            "engine": engine.snapshot(),
            "traces": {"decisions": decisions.sync(), "quality": quality.sync()},
            "wall_seconds": prior_wall + time.monotonic() - started,
            "complete": complete,
        }
        _checkpoint(checkpoint_path, payload)

    try:
        if old is None or not old["complete"]:
            partial = False
            for position, event in enumerate(_iter_events(canonical_dir, batch_size), start=1):
                if position <= engine.arrivals:
                    continue
                if max_arrivals is not None and engine.arrivals >= max_arrivals:
                    partial = True
                    break
                ready, notices = engine.ingest(event)
                for item in ready:
                    decisions.append(item)
                for item in notices:
                    quality.append(item)
                if engine.arrivals % replay_policy.checkpoint_every == 0:
                    save(False)
            if not partial:
                for item in engine.finish():
                    decisions.append(item)
            save(not partial)
        else:
            save(True)
    finally:
        decisions.close()
        quality.close()
    completed = _load_checkpoint(checkpoint_path)
    _write_atomic(
        root / "report.json",
        _report(root, identity, engine, completed["wall_seconds"], completed["complete"]),
    )
    return root


def verify_replay_report(root: Path) -> dict[str, Any]:
    report = _read_json(root / "report.json")
    checkpoint = _load_checkpoint(root / "checkpoint.json")
    if report["identity"] != checkpoint["identity"] or report["status"] != (
        "complete" if checkpoint["complete"] else "partial_checkpointed"
    ):
        raise ValueError("replay report/checkpoint mismatch")
    for name, digest in (
        ("decisions.jsonl", report["trace_sha256"]),
        ("quality.jsonl", report["quality_sha256"]),
    ):
        if sha256_file(root / name) != digest:
            raise ValueError(f"replay {name} hash mismatch")
    if report["decisions"] != checkpoint["engine"]["decisions"]:
        raise ValueError("replay decision count mismatch")
    return report


def verify_replay_parity(root: Path, feature_dir: Path) -> dict[str, Any]:
    """Compare every replayed feature row to the verified Phase 3 batch reference."""
    report = verify_replay_report(root)
    features = verify_features(feature_dir)
    if (
        features["identity"]["source_manifest_sha256"]
        != report["identity"]["canonical_manifest_sha256"]
    ):
        raise ValueError("reference features belong to another canonical source")
    if features["identity"]["spec"] != report["identity"]["feature_spec"]:
        raise ValueError("reference feature specification differs")
    if features["identity"]["split"] != report["identity"]["split_plan"]:
        raise ValueError("reference feature split differs")
    baseline_dir = report["identity"]["baseline_dir"]
    policy_path = report["identity"]["baseline_policy_path"]
    baseline_policy, robust_stats = _baseline(
        Path(baseline_dir) if baseline_dir else None,
        Path(policy_path) if policy_path else None,
        features["source_sha256"],
    )
    alert_states: dict[str, dict[str, AlertPersistence]] = {}

    def reference_rows() -> Iterator[dict[str, Any]]:
        for part in features["parts"]:
            parquet = pq.ParquetFile(feature_dir / part["path"])
            try:
                for batch in parquet.iter_batches(batch_size=10_000):
                    yield from batch.to_pylist()
            finally:
                parquet.close()

    reference = reference_rows()
    checked = 0
    with (root / "decisions.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            decision = json.loads(line)
            expected = next(reference, None)
            if expected is None:
                raise ValueError("replay has more decisions than reference features")
            actual = decision["features"]
            for name, value in actual.items():
                other = expected[name]
                if name == "event_time_source":
                    other = other.isoformat()
                if isinstance(value, float) and isinstance(other, float):
                    if not math.isclose(value, other, rel_tol=1e-10, abs_tol=1e-8):
                        raise ValueError(f"feature parity mismatch at row {checked}: {name}")
                elif value != other:
                    raise ValueError(f"feature parity mismatch at row {checked}: {name}")
            if decision["split"] != expected["split"]:
                raise ValueError(f"split parity mismatch at row {checked}")
            if baseline_policy is not None and robust_stats is not None:
                asset = expected["asset_id"]
                states = alert_states.setdefault(
                    asset,
                    {
                        name: AlertPersistence(baseline_policy)
                        for name in ("physical", "robust_mad")
                    },
                )
                for name in ("physical", "robust_mad"):
                    if expected["split"] not in ("validation", "test"):
                        score = Score(False, None, ())
                    elif name == "physical":
                        score = score_physical(expected, baseline_policy)
                    else:
                        score = score_robust(expected, baseline_policy, robust_stats)
                    active, episode, start = states[name].process(expected, score)
                    reference_score = {
                        "eligible": score.eligible,
                        "value": score.value,
                        "triggered_signals": list(score.triggered_signals),
                        "active": active,
                        "new_episode": episode,
                        "episode_start": start.isoformat() if start else None,
                    }
                    if decision["scores"][name] != reference_score:
                        raise ValueError(f"score parity mismatch at row {checked}: {name}")
            elif decision["scores"]:
                raise ValueError(f"unexpected scores at row {checked}")
            checked += 1
    if checked != report["decisions"]:
        raise ValueError("replay trace row count differs from report")
    return {
        "checked_rows": checked,
        "status": "matched",
        "float_abs_tolerance": 1e-8,
        "scores_checked": baseline_policy is not None,
    }
