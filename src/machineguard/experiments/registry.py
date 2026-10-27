"""Immutable local experiment catalog for existing research artifacts.

The catalog stores metadata and paths, never source bytes or executable models.
Every read rechecks the record and original artifact hashes.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np

from machineguard import __version__
from machineguard.anomaly.multivariate import run_multivariate, verify_multivariate
from machineguard.anomaly.pipeline import run_baselines, verify_baselines
from machineguard.core.provenance import sha256_file
from machineguard.features.pipeline import verify_features
from machineguard.prognostics.benchmark import run_benchmark, verify_benchmark

Kind = Literal["anomaly-baseline", "isolation-forest", "cmapss-rul"]
SCHEMA_VERSION = 1


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return cast(dict[str, Any], value)


def _required_inputs(kind: Kind, identity: dict[str, Any]) -> dict[str, str]:
    if kind == "cmapss-rul":
        return {"archive": identity["archive_sha256"], "policy": identity["policy_sha256"]}
    inputs = {
        "feature_version": identity["feature_manifest_sha256"],
        "policy": identity[
            "policy_sha256" if kind == "anomaly-baseline" else "model_policy_sha256"
        ],
        "report_windows": identity["report_windows_sha256"],
    }
    if kind == "isolation-forest":
        inputs.update(
            baseline_version=identity["baseline_manifest_sha256"],
            baseline_policy=identity["baseline_policy_sha256"],
        )
    return inputs


def _input_file(name: str, path: Path) -> Path:
    return path / "manifest.json" if name.endswith("_version") else path


def _check_inputs(inputs: dict[str, Any]) -> list[str]:
    problems = []
    for name, item in inputs.items():
        path = _input_file(name, Path(item["path"]))
        if not path.is_file():
            problems.append(f"{name}: missing bytes at {path}")
        elif sha256_file(path) != item["sha256"]:
            problems.append(f"{name}: SHA-256 mismatch at {path}")
        elif name == "feature_version":
            try:
                verify_features(Path(item["path"]))
            except (OSError, ValueError) as exc:
                problems.append(f"{name}: derived bytes unavailable or invalid: {exc}")
        elif name == "baseline_version":
            try:
                verify_baselines(Path(item["path"]))
            except (OSError, ValueError) as exc:
                problems.append(f"{name}: baseline bytes unavailable or invalid: {exc}")
    return problems


def _code_problems(kind: Kind, identity: dict[str, Any]) -> list[str]:
    problems = []
    if kind == "cmapss-rul":
        path = Path(__file__).parents[1] / "prognostics" / "benchmark.py"
        if identity["code_sha256"] != sha256_file(path):
            problems.append("benchmark implementation hash differs")
        checks = {
            "package_version": __version__,
            "numpy_version": np.__version__,
            "sklearn_version": version("scikit-learn"),
        }
    else:
        checks = {"builder_version": __version__}
        if kind == "anomaly-baseline":
            folder = Path(__file__).parents[1] / "anomaly"
            digest = hashlib.sha256(
                "".join(
                    sha256_file(folder / name) for name in ("statistical.py", "pipeline.py")
                ).encode()
            ).hexdigest()
        else:
            folder = Path(__file__).parents[1]
            paths = [
                folder / "anomaly" / "multivariate.py",
                folder / "anomaly" / "models.py",
                folder / "evaluation" / "events.py",
            ]
            digest = hashlib.sha256(
                "".join(sha256_file(path) for path in paths).encode()
            ).hexdigest()
            checks.update(numpy_version=np.__version__, sklearn_version=version("scikit-learn"))
            try:
                checks["scipy_version"] = version("scipy")
            except PackageNotFoundError:
                problems.append("SciPy is unavailable")
        if identity["builder_code_sha256"] != digest:
            problems.append("builder implementation hash differs")
    for key, actual in checks.items():
        if identity.get(key) != actual:
            problems.append(f"{key} differs: artifact={identity.get(key)}, current={actual}")
    return problems


def _artifact(kind: Kind, directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if kind == "cmapss-rul":
        report = verify_benchmark(directory)
        return _read_json(directory / "manifest.json"), report
    if kind == "anomaly-baseline":
        manifest = verify_baselines(directory)
    else:
        manifest = verify_multivariate(directory)
    return manifest, _read_json(directory / "evaluation.json")


def _signature(kind: Kind, data: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    if kind == "cmapss-rul":
        return {
            "dataset": "nasa-cmapss-classic",
            "target": data["target"],
            "unit": "operational cycles",
            "subsets": sorted(data["subsets"]),
            "protocol_sha256": hashlib.sha256(_canonical(data["protocol"])).hexdigest(),
            "folds_sha256": hashlib.sha256(
                _canonical({name: item["folds"] for name, item in data["subsets"].items()})
            ).hexdigest(),
        }
    return {
        "dataset": "metropt-3-uci",
        "target": "descriptive anomaly episodes; no failure probability",
        "unit": "episodes per observed day",
        "feature_manifest_sha256": identity["feature_manifest_sha256"],
        "report_windows_sha256": identity["report_windows_sha256"],
    }


def _metrics(kind: Kind, data: dict[str, Any]) -> dict[str, Any]:
    if kind == "cmapss-rul":
        return {
            subset: {
                split: {
                    model: values["mae_cycles"]
                    for model, values in data["subsets"][subset][split].items()
                }
                for split in ("validation", "publisher_test")
            }
            for subset in sorted(data["subsets"])
        }
    if kind == "anomaly-baseline":
        return {
            model: {
                split: values["off_report_episodes_per_observed_day"]
                for split, values in splits.items()
            }
            for model, splits in data["metrics"].items()
        }
    return {
        split: {
            model: values["off_report_episodes_per_observed_day"]
            for model, values in data["comparison"][split].items()
        }
        for split in ("validation", "test")
    }


def register_run(
    registry_root: Path, kind: Kind, artifact_dir: Path, input_paths: dict[str, Path]
) -> dict[str, Any]:
    """Register a verified artifact. Missing inputs are recorded honestly for later diagnosis."""
    manifest, data = _artifact(kind, artifact_dir)
    identity = cast(
        dict[str, Any], data["identity"] if kind == "cmapss-rul" else manifest["identity"]
    )
    expected = _required_inputs(kind, identity)
    if set(input_paths) != set(expected):
        raise ValueError(f"required input names: {', '.join(sorted(expected))}")
    inputs: dict[str, Any] = {}
    for name, digest in expected.items():
        path = input_paths[name].resolve()
        file = _input_file(name, path)
        if file.is_file() and sha256_file(file) != digest:
            raise ValueError(f"{name} SHA-256 differs from artifact identity")
        inputs[name] = {"path": str(path), "sha256": digest}
    if kind != "cmapss-rul" and input_paths["feature_version"].exists():
        verify_features(input_paths["feature_version"])
    if kind == "isolation-forest" and input_paths["baseline_version"].exists():
        verify_baselines(input_paths["baseline_version"])
    signature = _signature(kind, data, identity)
    if kind == "cmapss-rul":
        split_sha256 = signature["folds_sha256"]
        seed: int | None = data["protocol"]["seed"]
    else:
        feature_manifest = (
            _read_json(input_paths["feature_version"] / "manifest.json")
            if input_paths["feature_version"].exists()
            else None
        )
        split_sha256 = (
            hashlib.sha256(_canonical(feature_manifest["identity"]["split"])).hexdigest()
            if feature_manifest is not None
            else None
        )
        seed = (
            _read_json(artifact_dir / "model_metadata.json")["policy"]["random_state"]
            if kind == "isolation-forest"
            else None
        )
    artifact_path = artifact_dir.resolve()
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "artifact_dir": str(artifact_path),
        "artifact_manifest_sha256": sha256_file(artifact_path / "manifest.json"),
        "artifact_version_id": manifest["version_id"],
        "identity": identity,
        "dependency_lock_sha256": sha256_file(Path(__file__).parents[3] / "uv.lock"),
        "split_sha256": split_sha256,
        "seed": seed,
        "inputs": inputs,
        "signature": signature,
        "metrics": _metrics(kind, data),
        "promotion_status": "research_only",
        "promotion_reason": (
            "No deployment validation or calibrated industrial failure probability; "
            "test metrics are descriptive and cannot drive promotion."
        ),
    }
    run_id = hashlib.sha256(_canonical(record)).hexdigest()[:24]
    record["run_id"] = run_id
    root = registry_root / "runs"
    destination = root / run_id
    if destination.exists():
        if read_run(registry_root, run_id) != record:
            raise ValueError("existing run ID has different content")
        return record
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=root) as temp:
        staging = Path(temp) / run_id
        staging.mkdir()
        (staging / "record.json").write_bytes(_canonical(record) + b"\n")
        if destination.exists():
            raise ValueError("concurrent run registration detected")
        os.replace(staging, destination)
    return record


def read_run(registry_root: Path, run_id: str) -> dict[str, Any]:
    if len(run_id) != 24 or any(char not in "0123456789abcdef" for char in run_id):
        raise ValueError("invalid run ID")
    record = _read_json(registry_root / "runs" / run_id / "record.json")
    if record.get("schema_version") != SCHEMA_VERSION or record.get("run_id") != run_id:
        raise ValueError("unsupported registry schema or run ID mismatch")
    unsigned = {key: value for key, value in record.items() if key != "run_id"}
    if hashlib.sha256(_canonical(unsigned)).hexdigest()[:24] != run_id:
        raise ValueError("registry record hash mismatch")
    kind = cast(Kind, record["kind"])
    if kind not in ("anomaly-baseline", "isolation-forest", "cmapss-rul"):
        raise ValueError("unsupported artifact kind")
    artifact_dir = Path(record["artifact_dir"])
    if sha256_file(artifact_dir / "manifest.json") != record["artifact_manifest_sha256"]:
        raise ValueError("artifact manifest hash mismatch")
    manifest, data = _artifact(kind, artifact_dir)
    identity = data["identity"] if kind == "cmapss-rul" else manifest["identity"]
    if identity != record["identity"] or manifest["version_id"] != record["artifact_version_id"]:
        raise ValueError("artifact identity mismatch")
    if record["signature"] != _signature(kind, data, identity):
        raise ValueError("artifact schema or target mismatch")
    if record["metrics"] != _metrics(kind, data):
        raise ValueError("artifact metrics mismatch")
    return record


def list_runs(registry_root: Path) -> list[dict[str, Any]]:
    root = registry_root / "runs"
    if not root.exists():
        return []
    return [
        read_run(registry_root, item.name)
        for item in sorted(root.iterdir())
        if not item.name.startswith(".staging-")
    ]


def compare_runs(registry_root: Path, first_id: str, second_id: str) -> dict[str, Any]:
    first, second = read_run(registry_root, first_id), read_run(registry_root, second_id)
    if first["kind"] != second["kind"] or first["signature"] != second["signature"]:
        raise ValueError("incompatible model, dataset, target, schema, or evaluation protocol")
    return {
        "first": first_id,
        "second": second_id,
        "signature": first["signature"],
        "metrics": {first_id: first["metrics"], second_id: second["metrics"]},
        "promotion_status": "research_only",
    }


def reproduce_run(registry_root: Path, run_id: str, output_root: Path) -> dict[str, Any]:
    """Rebuild in a fresh output root, or identify the exact preflight blocker."""
    record = read_run(registry_root, run_id)
    kind = cast(Kind, record["kind"])
    reasons = _check_inputs(record["inputs"]) + _code_problems(kind, record["identity"])
    if sha256_file(Path(__file__).parents[3] / "uv.lock") != record["dependency_lock_sha256"]:
        reasons.append("dependency lockfile differs")
    if reasons:
        return {"run_id": run_id, "status": "blocked", "reasons": reasons}
    inputs = {name: Path(item["path"]) for name, item in record["inputs"].items()}
    if kind == "cmapss-rul":
        result = run_benchmark(inputs["archive"], output_root, inputs["policy"])
    elif kind == "anomaly-baseline":
        result = run_baselines(
            inputs["feature_version"], output_root, inputs["policy"], inputs["report_windows"]
        )
    else:
        result = run_multivariate(
            inputs["feature_version"],
            inputs["baseline_version"],
            output_root,
            inputs["policy"],
            inputs["baseline_policy"],
            inputs["report_windows"],
        )
    if result.name != record["artifact_version_id"]:
        raise ValueError("reproduction produced a different artifact identity")
    original_manifest, original_data = _artifact(kind, Path(record["artifact_dir"]))
    reproduced_manifest, reproduced_data = _artifact(kind, result)
    if kind == "cmapss-rul":
        matches = original_manifest["report_sha256"] == reproduced_manifest["report_sha256"]
    else:
        names = (
            ("baseline.json_sha256", "evaluation.json_sha256")
            if kind == "anomaly-baseline"
            else ("model_metadata.json_sha256", "evaluation.json_sha256")
        )
        matches = all(original_manifest[name] == reproduced_manifest[name] for name in names)
    if not matches or original_data != reproduced_data:
        raise ValueError("reproduced output differs from registered artifact")
    return {"run_id": run_id, "status": "reproduced", "version_dir": str(result)}
