"""Strict contracts for assumed maintenance scenarios; no model probability is accepted."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _fields(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} fields must be exactly {', '.join(sorted(expected))}")
    return value


def _decimal(value: object, label: str, *, maximum: Decimal = Decimal("1000000000")) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,9})(?:\.\d{1,6})?", value):
        raise ValueError(f"{label} must be a nonnegative decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid {label}") from exc
    if not result.is_finite() or result > maximum:
        raise ValueError(f"{label} exceeds supported bound")
    return result


def _positive(value: object, label: str, *, maximum: Decimal = Decimal("1000000000")) -> Decimal:
    result = _decimal(value, label, maximum=maximum)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


@dataclass(frozen=True, slots=True)
class AssumedRange:
    low: Decimal
    base: Decimal
    high: Decimal

    @classmethod
    def from_dict(
        cls, value: object, label: str, *, maximum: Decimal = Decimal("1000000000")
    ) -> AssumedRange:
        data = _fields(value, {"low", "base", "high"}, label)
        result = cls(
            *(
                _decimal(data[key], f"{label}.{key}", maximum=maximum)
                for key in ("low", "base", "high")
            )
        )
        if not result.low <= result.base <= result.high:
            raise ValueError(f"{label} must satisfy low <= base <= high")
        return result


@dataclass(frozen=True, slots=True)
class AssetAssumptions:
    asset_id: str
    failure_probability: AssumedRange
    preventive_effectiveness: AssumedRange
    preventive_cost: AssumedRange
    failure_repair_cost: AssumedRange
    downtime_cost_per_hour: AssumedRange
    planned_downtime_hours: Decimal
    unplanned_downtime_hours: Decimal
    criticality_multiplier: Decimal
    allowed_slots: tuple[str, ...]
    must_schedule: bool

    @classmethod
    def from_dict(cls, value: object) -> AssetAssumptions:
        expected = {
            "asset_id",
            "failure_probability",
            "preventive_effectiveness",
            "preventive_cost",
            "failure_repair_cost",
            "downtime_cost_per_hour",
            "planned_downtime_hours",
            "unplanned_downtime_hours",
            "criticality_multiplier",
            "allowed_slots",
            "must_schedule",
        }
        data = _fields(value, expected, "asset")
        asset_id = data["asset_id"]
        if not isinstance(asset_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", asset_id
        ):
            raise ValueError("asset_id must be a short stable identifier")
        slots = data["allowed_slots"]
        if (
            not isinstance(slots, list)
            or not slots
            or any(not isinstance(item, str) for item in slots)
            or len(set(slots)) != len(slots)
        ):
            raise ValueError("allowed_slots must be a nonempty list of unique slot IDs")
        if type(data["must_schedule"]) is not bool:
            raise ValueError("must_schedule must be boolean")
        probability = AssumedRange.from_dict(
            data["failure_probability"], "failure_probability", maximum=Decimal(1)
        )
        effectiveness = AssumedRange.from_dict(
            data["preventive_effectiveness"], "preventive_effectiveness", maximum=Decimal(1)
        )
        criticality = _positive(
            data["criticality_multiplier"], "criticality_multiplier", maximum=Decimal(100)
        )
        return cls(
            asset_id=asset_id,
            failure_probability=probability,
            preventive_effectiveness=effectiveness,
            preventive_cost=AssumedRange.from_dict(data["preventive_cost"], "preventive_cost"),
            failure_repair_cost=AssumedRange.from_dict(
                data["failure_repair_cost"], "failure_repair_cost"
            ),
            downtime_cost_per_hour=AssumedRange.from_dict(
                data["downtime_cost_per_hour"], "downtime_cost_per_hour"
            ),
            planned_downtime_hours=_decimal(
                data["planned_downtime_hours"], "planned_downtime_hours", maximum=Decimal(100000)
            ),
            unplanned_downtime_hours=_decimal(
                data["unplanned_downtime_hours"],
                "unplanned_downtime_hours",
                maximum=Decimal(100000),
            ),
            criticality_multiplier=criticality,
            allowed_slots=tuple(slots),
            must_schedule=data["must_schedule"],
        )


@dataclass(frozen=True, slots=True)
class CrewSlot:
    slot_id: str
    crew_capacity: int
    planned_cost_multiplier: Decimal

    @classmethod
    def from_dict(cls, value: object) -> CrewSlot:
        data = _fields(value, {"slot_id", "crew_capacity", "planned_cost_multiplier"}, "slot")
        slot_id = data["slot_id"]
        if not isinstance(slot_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", slot_id
        ):
            raise ValueError("slot_id must be a short stable identifier")
        capacity = data["crew_capacity"]
        if type(capacity) is not int or not 0 <= capacity <= 3:
            raise ValueError("crew_capacity must be an integer between 0 and 3")
        multiplier = _positive(data["planned_cost_multiplier"], "planned_cost_multiplier")
        if multiplier > 10:
            raise ValueError("planned_cost_multiplier must be <= 10")
        return cls(slot_id, capacity, multiplier)


@dataclass(frozen=True, slots=True)
class Scenario:
    currency: str
    horizon_hours: int
    assumptions_note: str
    assets: tuple[AssetAssumptions, ...]
    slots: tuple[CrewSlot, ...]

    @classmethod
    def from_dict(cls, value: object) -> Scenario:
        expected = {
            "schema_version",
            "scenario_kind",
            "currency",
            "horizon_hours",
            "assumptions_note",
            "assets",
            "slots",
        }
        data = _fields(value, expected, "scenario")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported scenario schema version")
        if data["scenario_kind"] != "hypothetical_assumptions":
            raise ValueError(
                "model-driven costs unavailable: only hypothetical_assumptions are accepted"
            )
        currency = data["currency"]
        if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
            raise ValueError("currency must be a three-letter uppercase code")
        horizon = data["horizon_hours"]
        if type(horizon) is not int or not 1 <= horizon <= 8760:
            raise ValueError("horizon_hours must be an integer from 1 to 8760")
        note = data["assumptions_note"]
        if not isinstance(note, str) or len(note.strip()) < 12:
            raise ValueError("assumptions_note must explain the hypothetical inputs")
        if not isinstance(data["assets"], list) or not 1 <= len(data["assets"]) <= 10:
            raise ValueError("scenario requires 1 to 10 assets")
        if not isinstance(data["slots"], list) or not 1 <= len(data["slots"]) <= 6:
            raise ValueError("scenario requires 1 to 6 crew slots")
        assets = tuple(
            sorted(
                (AssetAssumptions.from_dict(item) for item in data["assets"]),
                key=lambda item: item.asset_id,
            )
        )
        slots = tuple(
            sorted(
                (CrewSlot.from_dict(item) for item in data["slots"]), key=lambda item: item.slot_id
            )
        )
        if len({item.asset_id for item in assets}) != len(assets):
            raise ValueError("asset IDs must be unique")
        if len({item.slot_id for item in slots}) != len(slots):
            raise ValueError("slot IDs must be unique")
        available = {item.slot_id for item in slots}
        if any(set(item.allowed_slots) - available for item in assets):
            raise ValueError("asset references an unknown slot")
        return cls(currency, horizon, note.strip(), assets, slots)

    @classmethod
    def from_file(cls, path: Path) -> Scenario:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read scenario JSON: {exc}") from exc
        return cls.from_dict(data)
