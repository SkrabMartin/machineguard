"""Maintenance outputs are hypothetical arithmetic, never calibrated predictions."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from machineguard.cli import main
from machineguard.optimization.contracts import Scenario
from machineguard.optimization.scenarios import evaluate_scenario

EXAMPLE = Path("configs/maintenance_scenario.synthetic.json")


def _data() -> dict[str, object]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_example_is_hypothetical_and_deterministic() -> None:
    scenario = Scenario.from_dict(_data())
    first = evaluate_scenario(scenario)
    assert first == evaluate_scenario(Scenario.from_dict(_data()))
    assert first["evidence_status"] == "hypothetical_only"
    assert first["model_probability_status"] == "unavailable"
    assert first["decision_status"] == "human_review_required"
    assert first["schedule"]["status"] == "feasible"
    assert first["assumptions"]["assets"][0]["asset_id"] == "synthetic-pump-A"


def test_hand_calculated_costs_and_bounds() -> None:
    data = _data()
    asset = data["assets"][0]
    asset["failure_probability"] = {"low": "0.1", "base": "0.2", "high": "0.3"}
    asset["preventive_effectiveness"] = {"low": "0.4", "base": "0.5", "high": "0.6"}
    asset["failure_repair_cost"] = {"low": "1000", "base": "1000", "high": "1000"}
    asset["downtime_cost_per_hour"] = {"low": "100", "base": "100", "high": "100"}
    asset["preventive_cost"] = {"low": "100", "base": "100", "high": "100"}
    asset["planned_downtime_hours"] = "1"
    asset["unplanned_downtime_hours"] = "2"
    asset["criticality_multiplier"] = "1"
    asset["allowed_slots"] = ["day-1"]
    data["assets"] = [asset]
    data["slots"] = [data["slots"][0]]
    economics = evaluate_scenario(Scenario.from_dict(data))["asset_economics"][0]["slot_options"][0]
    assert economics["hypothetical_failure_consequence"]["base"] == "1200.00"
    assert economics["planned_maintenance_cost"]["base"] == "200.00"
    assert economics["expected_defer_cost_base"] == "240.00"
    assert economics["expected_maintain_cost_base"] == "320.00"
    assert economics["net_benefit_of_maintenance"] == {
        "low": "-152.00",
        "base": "-80.00",
        "high": "16.00",
    }
    assert economics["sensitivity"] == "changes_sign_with_assumptions"
    assert economics["break_even_probability_base_costs"] == "0.333333"


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"scenario_kind": "calibrated_model"}, "model-driven costs unavailable"),
        ({"schema_version": True}, "schema version"),
        ({"anomaly_score": "0.9"}, "fields must be exactly"),
    ],
)
def test_unqualified_inputs_rejected(change: dict[str, object], match: str) -> None:
    data = _data()
    data.update(change)
    with pytest.raises(ValueError, match=match):
        Scenario.from_dict(data)


@pytest.mark.parametrize("value", ["-0.1", "1.1", "NaN", "0.1oops", 0.2])
def test_invalid_probability_rejected(value: object) -> None:
    data = _data()
    data["assets"][0]["failure_probability"]["base"] = value
    with pytest.raises(ValueError, match="failure_probability"):
        Scenario.from_dict(data)


def test_reversed_probability_range_rejected() -> None:
    data = _data()
    data["assets"][0]["failure_probability"]["low"] = "0.99"
    with pytest.raises(ValueError, match="low <= base <= high"):
        Scenario.from_dict(data)


def test_numeric_bounds_and_long_decimal_rejected() -> None:
    data = _data()
    data["assets"][0]["planned_downtime_hours"] = "100001"
    with pytest.raises(ValueError, match="supported bound"):
        Scenario.from_dict(data)
    data["assets"][0]["planned_downtime_hours"] = "2"
    data["assets"][0]["failure_probability"]["base"] = "0.1234567"
    with pytest.raises(ValueError, match="decimal string"):
        Scenario.from_dict(data)


def test_infeasible_mandatory_capacity_and_valid_capacity() -> None:
    data = _data()
    data["slots"] = [data["slots"][0]]
    for asset in data["assets"]:
        asset["allowed_slots"] = ["day-1"]
        asset["must_schedule"] = True
    impossible = evaluate_scenario(Scenario.from_dict(data))["schedule"]
    assert impossible["status"] == "infeasible"
    assert impossible["assignments"] == []
    assert impossible["total_expected_cost_with_schedule"] is None
    data["slots"][0]["crew_capacity"] = 2
    possible = evaluate_scenario(Scenario.from_dict(data))["schedule"]
    assert possible["status"] == "feasible"
    assert sum(item["action"] == "maintain_in_slot" for item in possible["assignments"]) == 2


def test_deterministic_tie_prefers_first_asset() -> None:
    data = _data()
    data["slots"] = [data["slots"][0]]
    first = data["assets"][0]
    first["allowed_slots"] = ["day-1"]
    first["failure_probability"] = {"low": "1", "base": "1", "high": "1"}
    data["assets"] = [first, copy.deepcopy(first)]
    data["assets"][1]["asset_id"] = "synthetic-pump-B"
    assignments = evaluate_scenario(Scenario.from_dict(data))["schedule"]["assignments"]
    assert [item["action"] for item in assignments] == ["maintain_in_slot", "defer"]


def test_cli_outputs_provenance(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["simulate-maintenance", str(EXAMPLE)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output["scenario_sha256"]) == 64
    assert output["schedule"]["status"] == "feasible"


def test_cli_rejects_invalid_scenario(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["simulate-maintenance", str(path)])
    assert exc.value.code == 2
