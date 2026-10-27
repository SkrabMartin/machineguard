"""Observed Phase 1 source layouts; no data loading or label generation."""

from __future__ import annotations

from .contracts import DatasetContract, FieldContract, FieldRole, FieldType, TimeBasis


def _field(raw: str, canonical: str, role: FieldRole, unit: str | None = None) -> FieldContract:
    if role in {FieldRole.INDEX, FieldRole.ASSET_ID, FieldRole.CYCLE}:
        dtype = FieldType.INTEGER
    elif role is FieldRole.TIMESTAMP:
        dtype = FieldType.DATETIME
    else:
        dtype = FieldType.FLOAT
    return FieldContract(raw, canonical, role, dtype, unit)


METROPT3 = DatasetContract(
    dataset_id="metropt-3-uci",
    schema_version="1",
    source_format="csv",
    fields=(
        _field("", "source_row_index", FieldRole.INDEX),
        _field("timestamp", "event_time", FieldRole.TIMESTAMP),
        _field("TP2", "tp2", FieldRole.SENSOR, "bar"),
        _field("TP3", "tp3", FieldRole.SENSOR, "bar"),
        _field("H1", "h1", FieldRole.SENSOR, "bar"),
        _field("DV_pressure", "dv_pressure", FieldRole.SENSOR, "bar"),
        _field("Reservoirs", "reservoirs", FieldRole.SENSOR, "bar"),
        _field("Oil_temperature", "oil_temperature", FieldRole.SENSOR, "degC"),
        _field("Motor_current", "motor_current", FieldRole.SENSOR, "A"),
        _field("COMP", "comp", FieldRole.SENSOR, "state"),
        _field("DV_eletric", "dv_electric", FieldRole.SENSOR, "state"),
        _field("Towers", "towers", FieldRole.SENSOR, "state"),
        _field("MPG", "mpg", FieldRole.SENSOR, "state"),
        _field("LPS", "lps", FieldRole.SENSOR, "state"),
        _field("Pressure_switch", "pressure_switch", FieldRole.SENSOR, "state"),
        _field("Oil_level", "oil_level", FieldRole.SENSOR, "state"),
        _field("Caudal_impulses", "caudal_impulses", FieldRole.SENSOR, "pulse"),
    ),
    time_basis=TimeBasis.WALL_CLOCK,
    time_field="event_time",
    asset_field=None,
    timestamp_format="%Y-%m-%d %H:%M:%S",
    timezone_policy="source_naive_unresolved",
    has_header=True,
    label_status="unlabeled; four source-reported fault windows require event audit",
)

CMAPSS_CLASSIC = DatasetContract(
    dataset_id="nasa-cmapss-classic",
    schema_version="1",
    source_format="whitespace",
    fields=(
        _field("unit_number", "unit_id", FieldRole.ASSET_ID),
        _field("cycle", "cycle", FieldRole.CYCLE, "cycle"),
        *(_field(f"setting_{i}", f"setting_{i}", FieldRole.SETTING) for i in range(1, 4)),
        *(_field(f"sensor_{i}", f"sensor_{i}", FieldRole.SENSOR) for i in range(1, 22)),
    ),
    time_basis=TimeBasis.CYCLES,
    time_field="cycle",
    asset_field="unit_id",
    timestamp_format=None,
    timezone_policy=None,
    has_header=False,
    label_status="training trajectories run to failure; test terminal RUL in separate truth files",
)

DATASET_CONTRACTS = {contract.dataset_id: contract for contract in (METROPT3, CMAPSS_CLASSIC)}
