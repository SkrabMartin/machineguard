"""Train-only state-conditional Isolation Forest with explicit preprocessing."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sklearn.ensemble import IsolationForest  # type: ignore[import-untyped]

SIGNALS = (
    "tp3_mean_5m",
    "tp3_std_5m",
    "tp3_minus_reservoirs",
    "motor_current_mean_5m",
    "oil_temperature_mean_60m",
    "loaded_run_elapsed_capped_s",
)


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    version: str
    feature_version_id: str
    baseline_version_id: str
    signals: tuple[str, ...]
    contamination: float
    n_estimators: int
    max_samples: int
    random_state: int
    min_group_samples: int
    diagnostic_episode_sample_size: int
    selection_rationale: str

    @classmethod
    def from_json_file(cls, path: Path) -> ModelPolicy:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise ValueError("Isolation Forest policy fields do not match schema")
        data["signals"] = tuple(data["signals"])
        return cls(**data)

    def __post_init__(self) -> None:
        if not self.version or not self.feature_version_id or not self.baseline_version_id:
            raise ValueError("model policy identity is required")
        if self.signals != SIGNALS or not self.selection_rationale:
            raise ValueError("model signals and selection rationale must match approved protocol")
        if not math.isfinite(self.contamination) or not 0 < self.contamination <= 0.5:
            raise ValueError("contamination must be in (0, 0.5]")
        if (
            self.n_estimators < 1
            or self.max_samples < 2
            or self.min_group_samples < self.max_samples
            or self.diagnostic_episode_sample_size < 1
            or self.random_state < 0
        ):
            raise ValueError("invalid tree, training, diagnostic, or seed policy")


@dataclass(slots=True)
class TrainedState:
    median: NDArray[np.float64]
    scale: NDArray[np.float64]
    model: IsolationForest
    train_rows: int

    def transform(self, matrix: NDArray[np.float64]) -> NDArray[np.float32]:
        if matrix.ndim != 2 or matrix.shape[1] != len(SIGNALS):
            raise ValueError("feature matrix shape mismatch")
        if not np.isfinite(matrix).all():
            raise ValueError("feature matrix has nonfinite values")
        return np.asarray((matrix - self.median) / self.scale, dtype=np.float32)

    def decision_function(self, matrix: NDArray[np.float64]) -> NDArray[np.float64]:
        return np.asarray(self.model.decision_function(self.transform(matrix)), dtype=np.float64)

    def diagnostic_deltas(self, vector: NDArray[np.float64]) -> NDArray[np.float64]:
        """Positive delta means median replacement makes the row less anomalous."""
        original = self.decision_function(vector.reshape(1, -1))[0]
        trial = np.repeat(vector.reshape(1, -1), len(SIGNALS), axis=0)
        for index in range(len(SIGNALS)):
            trial[index, index] = self.median[index]
        return np.asarray(self.decision_function(trial) - original, dtype=np.float64)

    def metadata(self) -> dict[str, Any]:
        return {
            "train_rows": self.train_rows,
            "median": self.median.tolist(),
            "scale_iqr": self.scale.tolist(),
            "decision_offset": float(self.model.offset_),
            "trees": len(self.model.estimators_),
        }


def fit_state(matrix: NDArray[np.float64], policy: ModelPolicy) -> TrainedState:
    if matrix.ndim != 2 or matrix.shape[1] != len(SIGNALS):
        raise ValueError("training matrix shape mismatch")
    if matrix.shape[0] < policy.min_group_samples:
        raise ValueError("insufficient state-specific training samples")
    if not np.isfinite(matrix).all():
        raise ValueError("training matrix has nonfinite values")
    median = np.asarray(np.median(matrix, axis=0), dtype=np.float64)
    quartiles = np.quantile(matrix, (0.25, 0.75), axis=0)
    iqr = np.asarray(quartiles[1] - quartiles[0], dtype=np.float64)
    scale = np.where(iqr > 0, iqr, 1.0)
    model = IsolationForest(
        n_estimators=policy.n_estimators,
        max_samples=policy.max_samples,
        contamination=policy.contamination,
        random_state=policy.random_state,
        n_jobs=1,
    )
    fitted = TrainedState(median, scale, model, matrix.shape[0])
    model.fit(fitted.transform(matrix))
    return fitted
