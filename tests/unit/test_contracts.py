"""Small synthetic contract examples; no publisher dataset bytes are committed."""

from datetime import datetime

import pytest

from machineguard.core.catalog import CMAPSS_CLASSIC, METROPT3
from machineguard.core.contracts import (
    DatasetContract,
    FieldContract,
    FieldRole,
    FieldType,
    TimeBasis,
)
from machineguard.core.events import (
    Measurement,
    RemainingUsefulLifeTruth,
    ReportedEventWindow,
    SplitPolicy,
    TelemetryObservation,
)


def test_observed_metropt_header_and_width() -> None:
    observed = (
        "",
        "timestamp",
        "TP2",
        "TP3",
        "H1",
        "DV_pressure",
        "Reservoirs",
        "Oil_temperature",
        "Motor_current",
        "COMP",
        "DV_eletric",
        "Towers",
        "MPG",
        "LPS",
        "Pressure_switch",
        "Oil_level",
        "Caudal_impulses",
    )
    METROPT3.validate_header(observed)
    METROPT3.validate_width(17)
    assert METROPT3.timezone_policy == "source_naive_unresolved"
    with pytest.raises(ValueError, match="Header mismatch"):
        METROPT3.validate_header(observed[:-1] + ("Caudal_impulse",))
    with pytest.raises(ValueError, match="Expected 17"):
        METROPT3.validate_width(16)


def test_cmapss_positional_contract() -> None:
    CMAPSS_CLASSIC.validate_width(26)
    assert CMAPSS_CLASSIC.asset_field == "unit_id"
    assert CMAPSS_CLASSIC.time_basis is TimeBasis.CYCLES
    with pytest.raises(ValueError, match="no header"):
        CMAPSS_CLASSIC.validate_header(("unit_number",))


def test_reject_duplicate_or_inconsistent_contract() -> None:
    field = FieldContract("timestamp", "event_time", FieldRole.TIMESTAMP, FieldType.DATETIME)
    with pytest.raises(ValueError, match="unique"):
        DatasetContract(
            "x",
            "1",
            "csv",
            (field, field),
            TimeBasis.WALL_CLOCK,
            "event_time",
            None,
            "%Y",
            "unresolved",
            True,
            "unlabeled",
        )
    with pytest.raises(ValueError, match="timestamp field"):
        DatasetContract(
            "x",
            "1",
            "csv",
            (FieldContract("x", "x", FieldRole.SENSOR, FieldType.FLOAT),),
            TimeBasis.WALL_CLOCK,
            "x",
            None,
            "%Y",
            "unresolved",
            True,
            "unlabeled",
        )
    with pytest.raises(ValueError, match="unnamed source index"):
        FieldContract("", "x", FieldRole.SENSOR, FieldType.FLOAT)
    with pytest.raises(ValueError, match="datetimes"):
        FieldContract("timestamp", "event_time", FieldRole.TIMESTAMP, FieldType.FLOAT)


def test_event_window_requires_order_and_consistent_timezone() -> None:
    base = ("metropt-3-uci", "report-1")
    start = datetime(2020, 4, 18)
    end = datetime(2020, 4, 19)
    event = ReportedEventWindow(*base, start, end, "air leak", "UCI PDF")
    assert event.end > event.start
    with pytest.raises(ValueError, match="positive duration"):
        ReportedEventWindow(*base, end, start, "air leak", "UCI PDF")
    with pytest.raises(ValueError, match="timezone policy"):
        ReportedEventWindow(*base, start, datetime.now().astimezone(), "air leak", "UCI PDF")


def test_split_policy_requires_evidence() -> None:
    with pytest.raises(ValueError, match="required"):
        SplitPolicy("metropt-3-uci", None, "event_time", "", "pending")


def test_canonical_observation_time_basis_and_measurements() -> None:
    sample = TelemetryObservation(
        dataset_id="synthetic-test-source",
        asset_id="test-machine",
        source_sha256="a" * 64,
        time_basis=TimeBasis.CYCLES,
        event_time=None,
        cycle=1,
        measurements=(Measurement("temperature", 25.0, "degC"),),
    )
    assert sample.cycle == 1
    with pytest.raises(ValueError, match="positive cycle"):
        TelemetryObservation(
            "synthetic-test-source",
            "test-machine",
            "a" * 64,
            TimeBasis.CYCLES,
            None,
            0,
            sample.measurements,
        )
    with pytest.raises(ValueError, match="unique"):
        TelemetryObservation(
            "synthetic-test-source",
            "test-machine",
            "a" * 64,
            TimeBasis.CYCLES,
            None,
            1,
            sample.measurements * 2,
        )
    with pytest.raises(ValueError, match="finite"):
        Measurement("temperature", float("nan"), "degC")


def test_rul_truth_is_cycle_based_and_nonnegative() -> None:
    truth = RemainingUsefulLifeTruth("synthetic-test-source", "engine-1", 100, 20, "test truth")
    assert truth.remaining_cycles == 20
    with pytest.raises(ValueError, match="RUL cannot be negative"):
        RemainingUsefulLifeTruth("synthetic-test-source", "engine-1", 100, -1, "test truth")
