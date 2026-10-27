"""Synthetic Isolation Forest comparison with verified Phase 3/4 lineage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_anomaly_pipeline import _feature_fixture

from machineguard.anomaly.multivariate import run_multivariate, verify_multivariate
from machineguard.anomaly.pipeline import run_baselines


def _inputs(
    tmp_path: Path, validation_pressure: float = 20.0
) -> tuple[Path, Path, Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path("configs/metropt_feature_spec.json").read_text())
    spec["min_long_samples"] = 2
    spec_path = tmp_path / "feature_spec.json"
    spec_path.write_text(json.dumps(spec))
    feature_dir, baseline_policy, windows = _feature_fixture(
        tmp_path, validation_pressure, spec_path
    )
    baseline_dir = run_baselines(
        feature_dir, tmp_path / "baselines", baseline_policy, windows, batch_size=3
    )
    policy = json.loads(Path("configs/metropt_isolation_forest.json").read_text())
    policy.update(
        {
            "feature_version_id": feature_dir.name,
            "baseline_version_id": baseline_dir.name,
            "n_estimators": 8,
            "max_samples": 2,
            "min_group_samples": 2,
            "diagnostic_episode_sample_size": 3,
        }
    )
    policy_path = tmp_path / "model_policy.json"
    policy_path.write_text(json.dumps(policy))
    return feature_dir, baseline_dir, policy_path, baseline_policy, windows


def test_multivariate_lineage_reproducibility_and_train_isolation(tmp_path: Path) -> None:
    feature, baseline, policy, baseline_policy, windows = _inputs(tmp_path)
    output = run_multivariate(
        feature, baseline, tmp_path / "models", policy, baseline_policy, windows, batch_size=3
    )
    manifest = verify_multivariate(output)
    metadata = json.loads((output / "model_metadata.json").read_text())
    evaluation = json.loads((output / "evaluation.json").read_text())
    assert manifest["version_id"] == output.name
    assert metadata["fit_summary"]["complete_train_rows_by_state"] == {"0": 2, "1": 4}
    assert metadata["serialization_policy"].startswith("No executable model")
    assert evaluation["metrics"]["validation"]["rows"] == 3
    assert evaluation["metrics"]["test"]["rows"] == 3
    assert evaluation["comparison"]["validation"]["physical"]["episodes"] >= 0
    assert (
        run_multivariate(
            feature, baseline, tmp_path / "models", policy, baseline_policy, windows, batch_size=3
        )
        == output
    )
    independent = run_multivariate(
        feature,
        baseline,
        tmp_path / "independent",
        policy,
        baseline_policy,
        windows,
        batch_size=3,
    )
    other_manifest = verify_multivariate(independent)
    assert manifest["model_metadata.json_sha256"] == other_manifest["model_metadata.json_sha256"]
    assert manifest["evaluation.json_sha256"] == other_manifest["evaluation.json_sha256"]
    changed = _inputs(tmp_path / "changed", validation_pressure=200.0)
    changed_output = run_multivariate(
        changed[0],
        changed[1],
        tmp_path / "changed_models",
        changed[2],
        changed[3],
        changed[4],
        batch_size=3,
    )
    changed_metadata = json.loads((changed_output / "model_metadata.json").read_text())
    assert metadata["states"] == changed_metadata["states"]
    (output / "evaluation.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_multivariate(output)


def test_wrong_baseline_binding_rejected(tmp_path: Path) -> None:
    feature, baseline, policy, baseline_policy, windows = _inputs(tmp_path)
    data = json.loads(policy.read_text())
    data["baseline_version_id"] = "wrong"
    policy.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="baseline version mismatch"):
        run_multivariate(
            feature,
            baseline,
            tmp_path / "models",
            policy,
            baseline_policy,
            windows,
            batch_size=3,
        )
    assert not list((tmp_path / "models").rglob("manifest.json"))
