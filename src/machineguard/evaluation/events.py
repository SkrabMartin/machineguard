"""Fixed-protocol alert episode and uncertain report-window summaries."""

from __future__ import annotations

import math
from array import array
from datetime import datetime, timedelta
from typing import Any

from machineguard.anomaly.statistical import AlertPersistence, BaselinePolicy, Score
from machineguard.features.eda import ReportWindow

_SPLITS = ("validation", "test")


def outside_report_padding(
    timestamp: datetime, windows: tuple[ReportWindow, ...], seconds: int
) -> bool:
    margin = timedelta(seconds=seconds)
    return all(
        not window.start - margin <= timestamp < window.end_exclusive + margin for window in windows
    )


class EventEvaluation:
    """Score fixed validation/test periods with Phase 4 persistence and denominator rules."""

    def __init__(self, baseline_policy: BaselinePolicy, windows: tuple[ReportWindow, ...]) -> None:
        self.policy = baseline_policy
        self.windows = windows
        self.tracker = AlertPersistence(baseline_policy)
        self.metrics: dict[str, dict[str, Any]] = {
            split: {
                "rows": 0,
                "eligible_rows": 0,
                "unscorable_rows": 0,
                "raw_alert_rows": 0,
                "active_alert_rows": 0,
                "episodes": 0,
                "off_report_episode_starts": 0,
                "observed_off_report_seconds": 0,
                "active_alert_rows_by_state": {"0": 0, "1": 0},
                "decision_scores": array("d"),
            }
            for split in _SPLITS
        }
        self.events: list[dict[str, Any]] = [
            {
                "description": window.description,
                "start": window.start.isoformat(),
                "end_exclusive": window.end_exclusive.isoformat(),
                "split": None,
                "active_rows_inclusive_minute": 0,
                "active_rows_exact_endpoint": 0,
                "active_rows_preceding_hour": 0,
                "first_active_in_window": None,
                "first_active_preceding_hour": None,
            }
            for window in windows
        ]
        self._prior: tuple[datetime, str, bool] | None = None

    def observe(self, row: dict[str, Any], decision_score: float | None) -> bool:
        """Return whether this row starts an active episode."""
        timestamp: datetime = row["event_time_source"]
        split: str = row["split"]
        eligible = decision_score is not None and math.isfinite(decision_score)
        score = Score(
            eligible=eligible,
            value=decision_score,
            triggered_signals=("isolation_forest",)
            if eligible and decision_score is not None and decision_score < 0
            else (),
        )
        active, started, _ = self.tracker.process(row, score)
        if split not in _SPLITS:
            self._prior = None
            return False
        metric = self.metrics[split]
        metric["rows"] += 1
        metric["eligible_rows"] += int(eligible)
        metric["unscorable_rows"] += int(not eligible)
        metric["raw_alert_rows"] += int(score.raw_alert)
        metric["active_alert_rows"] += int(active)
        metric["episodes"] += int(started)
        if active:
            metric["active_alert_rows_by_state"][str(row["operating_load_flag"])] += 1
        if eligible:
            metric["decision_scores"].append(decision_score)
        off_report = outside_report_padding(
            timestamp, self.windows, self.policy.report_exclusion_seconds
        )
        if started and off_report:
            metric["off_report_episode_starts"] += 1
        if (
            eligible
            and off_report
            and self._prior is not None
            and self._prior[1] == split
            and self._prior[2]
            and 0
            < (timestamp - self._prior[0]).total_seconds()
            <= self.policy.max_contiguous_seconds
        ):
            metric["observed_off_report_seconds"] += int(
                (timestamp - self._prior[0]).total_seconds()
            )
        self._prior = (timestamp, split, eligible and off_report)
        for index, window in enumerate(self.windows):
            event = self.events[index]
            if window.start <= timestamp < window.end_exclusive:
                event["split"] = split
                if active:
                    event["active_rows_inclusive_minute"] += 1
                    if event["first_active_in_window"] is None:
                        event["first_active_in_window"] = timestamp.isoformat()
                    if timestamp <= window.end_exclusive - timedelta(minutes=1):
                        event["active_rows_exact_endpoint"] += 1
            elif window.start - timedelta(hours=1) <= timestamp < window.start and active:
                event["active_rows_preceding_hour"] += 1
                if event["first_active_preceding_hour"] is None:
                    event["first_active_preceding_hour"] = timestamp.isoformat()
        return started

    def result(self) -> dict[str, Any]:
        from numpy import quantile  # local import keeps evaluation contract lightweight

        for split in _SPLITS:
            metric = self.metrics[split]
            seconds = metric["observed_off_report_seconds"]
            metric["off_report_episodes_per_observed_day"] = (
                metric["off_report_episode_starts"] * 86400 / seconds if seconds else None
            )
            values = metric.pop("decision_scores")
            metric["decision_score_quantiles"] = (
                {
                    name: float(value)
                    for name, value in zip(
                        ("min", "p01", "p05", "median", "p95", "max"),
                        quantile(values, (0, 0.01, 0.05, 0.5, 0.95, 1)),
                        strict=True,
                    )
                }
                if values
                else None
            )
        for event in self.events:
            event["any_active_inclusive_minute"] = event["active_rows_inclusive_minute"] > 0
            event["any_active_exact_endpoint"] = event["active_rows_exact_endpoint"] > 0
            first = event["first_active_preceding_hour"] or event["first_active_in_window"]
            event["first_active_relative_to_report_start_seconds"] = (
                int(
                    (
                        datetime.fromisoformat(first) - datetime.fromisoformat(event["start"])
                    ).total_seconds()
                )
                if first is not None
                else None
            )
        return {
            "metrics": self.metrics,
            "reported_windows": self.events,
            "metric_caveat": (
                "Off-report episodes per observed day is a false-alarm proxy, not a verified "
                "false-positive rate. Report-window coincidence and relative timing are not "
                "event recall or true warning lead time: onset and missed reports are uncertain."
            ),
            "denominator_policy": (
                "Adjacent eligible intervals <= max_contiguous_seconds within each split, "
                "outside report windows +/- exclusion padding."
            ),
            "endpoint_sensitivity": (
                "Inclusive-minute end uses [start, next minute); exact-endpoint uses "
                "[start, listed endpoint instant]."
            ),
        }
