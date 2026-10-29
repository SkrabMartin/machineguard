"""Transparent arithmetic and exact small-scenario crew allocation.

All probabilities and costs are user-supplied hypotheses. This module never
accepts telemetry scores or makes an observed savings claim.
"""

from __future__ import annotations

from decimal import Decimal
from functools import cache
from typing import Any

from .contracts import AssetAssumptions, AssumedRange, CrewSlot, Scenario

_CENT = Decimal("0.01")
_SIX = Decimal("0.000001")


def _money(value: Decimal) -> str:
    return format(value.quantize(_CENT), ".2f")


def _range_values(value: AssumedRange) -> dict[str, str]:
    return {"low": str(value.low), "base": str(value.base), "high": str(value.high)}


def _at(value: AssumedRange, level: str) -> Decimal:
    return {"low": value.low, "base": value.base, "high": value.high}[level]


def _assumptions(scenario: Scenario) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scenario_kind": "hypothetical_assumptions",
        "currency": scenario.currency,
        "horizon_hours": scenario.horizon_hours,
        "assumptions_note": scenario.assumptions_note,
        "assets": [
            {
                "asset_id": asset.asset_id,
                "failure_probability": _range_values(asset.failure_probability),
                "preventive_effectiveness": _range_values(asset.preventive_effectiveness),
                "preventive_cost": _range_values(asset.preventive_cost),
                "failure_repair_cost": _range_values(asset.failure_repair_cost),
                "downtime_cost_per_hour": _range_values(asset.downtime_cost_per_hour),
                "planned_downtime_hours": str(asset.planned_downtime_hours),
                "unplanned_downtime_hours": str(asset.unplanned_downtime_hours),
                "criticality_multiplier": str(asset.criticality_multiplier),
                "allowed_slots": list(asset.allowed_slots),
                "must_schedule": asset.must_schedule,
            }
            for asset in scenario.assets
        ],
        "slots": [
            {
                "slot_id": slot.slot_id,
                "crew_capacity": slot.crew_capacity,
                "planned_cost_multiplier": str(slot.planned_cost_multiplier),
            }
            for slot in scenario.slots
        ],
    }


def _consequence(asset: AssetAssumptions, level: str) -> Decimal:
    return _at(asset.failure_repair_cost, level) + (
        asset.unplanned_downtime_hours
        * _at(asset.downtime_cost_per_hour, level)
        * asset.criticality_multiplier
    )


def _planned(asset: AssetAssumptions, slot: CrewSlot, level: str) -> Decimal:
    return slot.planned_cost_multiplier * (
        _at(asset.preventive_cost, level)
        + asset.planned_downtime_hours * _at(asset.downtime_cost_per_hour, level)
    )


def _net(asset: AssetAssumptions, slot: CrewSlot, level: str) -> Decimal:
    return _at(asset.failure_probability, level) * _at(
        asset.preventive_effectiveness, level
    ) * _consequence(asset, level) - _planned(asset, slot, level)


def _slot_economics(asset: AssetAssumptions, slot: CrewSlot) -> dict[str, Any]:
    probability = asset.failure_probability
    effect = asset.preventive_effectiveness
    consequence = {level: _consequence(asset, level) for level in ("low", "base", "high")}
    planned = {level: _planned(asset, slot, level) for level in ("low", "base", "high")}
    defer_base = probability.base * consequence["base"]
    maintain_base = planned["base"] + probability.base * (1 - effect.base) * consequence["base"]
    net_low = probability.low * effect.low * consequence["low"] - planned["high"]
    net_base = defer_base - maintain_base
    net_high = probability.high * effect.high * consequence["high"] - planned["low"]
    denominator = effect.base * consequence["base"]
    break_even = planned["base"] / denominator if denominator > 0 else None
    sensitivity = (
        "positive_across_assumed_bounds"
        if net_low > 0
        else "negative_across_assumed_bounds"
        if net_high < 0
        else "changes_sign_with_assumptions"
    )
    return {
        "slot_id": slot.slot_id,
        "hypothetical_failure_consequence": {
            key: _money(value) for key, value in consequence.items()
        },
        "planned_maintenance_cost": {key: _money(value) for key, value in planned.items()},
        "expected_defer_cost_base": _money(defer_base),
        "expected_maintain_cost_base": _money(maintain_base),
        "net_benefit_of_maintenance": {
            "low": _money(net_low),
            "base": _money(net_base),
            "high": _money(net_high),
        },
        "break_even_probability_base_costs": (
            str(break_even.quantize(_SIX)) if break_even is not None else None
        ),
        "break_even_within_unit_interval": break_even is not None and break_even <= 1,
        "sensitivity": sensitivity,
        "uncertainty_meaning": "box bounds across stated assumptions; not a confidence interval",
    }


def _optimal_assignment(scenario: Scenario) -> tuple[Decimal, tuple[str | None, ...]] | None:
    assets = scenario.assets
    slots = scenario.slots
    slot_index = {slot.slot_id: position for position, slot in enumerate(slots)}
    initial_capacity = tuple(slot.crew_capacity for slot in slots)

    @cache
    def solve(
        position: int, capacity: tuple[int, ...]
    ) -> tuple[Decimal, tuple[str | None, ...]] | None:
        if position == len(assets):
            return Decimal(0), ()
        asset = assets[position]
        options: list[str | None] = []
        options.extend(sorted(asset.allowed_slots))
        if not asset.must_schedule:
            options = [None, *options]
        best: tuple[Decimal, tuple[str | None, ...]] | None = None
        for chosen in options:
            remaining = list(capacity)
            gain = Decimal(0)
            if chosen is not None:
                index = slot_index[chosen]
                if remaining[index] == 0:
                    continue
                remaining[index] -= 1
                gain = _net(asset, slots[index], "base")
            tail = solve(position + 1, tuple(remaining))
            if tail is None:
                continue
            candidate = (gain + tail[0], (chosen, *tail[1]))
            if (
                best is None
                or candidate[0] > best[0]
                or (
                    candidate[0] == best[0]
                    and (
                        sum(item is not None for item in candidate[1]),
                        tuple("~" if item is None else item for item in candidate[1]),
                    )
                    < (
                        sum(item is not None for item in best[1]),
                        tuple("~" if item is None else item for item in best[1]),
                    )
                )
            ):
                best = candidate
        return best

    return solve(0, initial_capacity)


def evaluate_scenario(scenario: Scenario) -> dict[str, Any]:
    """Calculate assumed costs and an exact bounded schedule; no real recommendation."""
    per_asset = [
        {
            "asset_id": asset.asset_id,
            "slot_options": [
                _slot_economics(asset, slot)
                for slot in scenario.slots
                if slot.slot_id in asset.allowed_slots
            ],
        }
        for asset in scenario.assets
    ]
    defer_total = sum(
        (asset.failure_probability.base * _consequence(asset, "base") for asset in scenario.assets),
        Decimal(0),
    )
    optimum = _optimal_assignment(scenario)
    if optimum is None:
        schedule: dict[str, Any] = {
            "status": "infeasible",
            "reason": "mandatory assets exceed allowed crew slot capacity",
            "mandatory_assets": [
                asset.asset_id for asset in scenario.assets if asset.must_schedule
            ],
            "assignments": [],
            "total_expected_cost_with_schedule": None,
            "total_nominal_net_benefit": None,
        }
    else:
        gain, choices = optimum
        schedule = {
            "status": "feasible",
            "objective": "maximize nominal net benefit within crew capacity and allowed slots",
            "assignments": [
                {
                    "asset_id": asset.asset_id,
                    "action": "maintain_in_slot" if choice is not None else "defer",
                    "slot_id": choice,
                    "nominal_net_benefit": _money(
                        _net(
                            asset,
                            next(slot for slot in scenario.slots if slot.slot_id == choice),
                            "base",
                        )
                        if choice is not None
                        else Decimal(0)
                    ),
                }
                for asset, choice in zip(scenario.assets, choices, strict=True)
            ],
            "total_expected_cost_with_schedule": _money(defer_total - gain),
            "total_nominal_net_benefit": _money(gain),
        }
    return {
        "schema_version": 1,
        "evidence_status": "hypothetical_only",
        "model_probability_status": "unavailable",
        "decision_status": "human_review_required",
        "currency": scenario.currency,
        "horizon_hours": scenario.horizon_hours,
        "assumptions": _assumptions(scenario),
        "asset_economics": per_asset,
        "total_expected_cost_if_all_deferred": _money(defer_total),
        "schedule": schedule,
        "limitations": [
            "Every probability, cost, effectiveness, downtime, and capacity is assumed.",
            "One failure mode and at most one failure per asset per horizon are assumed.",
            "Risk and effectiveness do not vary by slot; only planned cost does.",
            "No observed industrial savings, calibrated risk, or automatic work order is implied.",
        ],
    }
