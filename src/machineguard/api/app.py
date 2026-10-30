"""Read-oriented local API over verified, explicitly indexed research artifacts."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from machineguard.api.catalog import Catalog, CatalogError
from machineguard.core.catalog import METROPT3
from machineguard.optimization.contracts import Scenario
from machineguard.optimization.scenarios import evaluate_scenario
from machineguard.storage.parquet import ASSET_ID, iter_canonical_batches

_SENSORS = {field.canonical_name: field.unit for field in METROPT3.fields[2:]}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RangePayload(_Strict):
    low: str = Field(max_length=18)
    base: str = Field(max_length=18)
    high: str = Field(max_length=18)


class AssetPayload(_Strict):
    asset_id: str = Field(max_length=64)
    failure_probability: RangePayload
    preventive_effectiveness: RangePayload
    preventive_cost: RangePayload
    failure_repair_cost: RangePayload
    downtime_cost_per_hour: RangePayload
    planned_downtime_hours: str = Field(max_length=18)
    unplanned_downtime_hours: str = Field(max_length=18)
    criticality_multiplier: str = Field(max_length=18)
    allowed_slots: list[str] = Field(min_length=1, max_length=6)
    must_schedule: bool


class SlotPayload(_Strict):
    slot_id: str = Field(max_length=64)
    crew_capacity: int
    planned_cost_multiplier: str = Field(max_length=18)


class ScenarioPayload(_Strict):
    schema_version: Literal[1]
    scenario_kind: Literal["hypothetical_assumptions"]
    currency: str = Field(max_length=3)
    horizon_hours: int
    assumptions_note: str = Field(min_length=12, max_length=1000)
    assets: list[AssetPayload] = Field(min_length=1, max_length=10)
    slots: list[SlotPayload] = Field(min_length=1, max_length=6)


class ArtifactRecord(_Strict):
    artifact_id: str
    kind: str
    manifest_sha256: str
    summary: dict[str, Any]


class ArtifactPage(_Strict):
    items: list[ArtifactRecord]
    offset: int
    limit: int
    total: int


class AssetRecord(_Strict):
    asset_id: str
    dataset_artifact_id: str
    dataset_id: str
    time_basis: str
    source_sha256: str


class AssetPage(_Strict):
    items: list[AssetRecord]
    offset: int
    limit: int
    total: int


class QualityResponse(_Strict):
    quality: dict[str, Any]
    provenance: ArtifactRecord
    time_basis: str


class AlertsResponse(_Strict):
    metrics: dict[str, Any]
    reported_windows: dict[str, Any]
    metric_caveat: str
    evidence_status: Literal["descriptive_anomaly_only"]
    provenance: ArtifactRecord


class RulResponse(_Strict):
    dataset_id: Literal["nasa-cmapss-classic"]
    target: str
    domain: str
    subsets: dict[str, Any]
    provenance: ArtifactRecord
    transfer_status: Literal["not_validated_for_metropt_or_industrial_compressors"]


class UnavailableResponse(_Strict):
    status: Literal["unavailable"]
    reason: str


class ScenarioResponse(_Strict):
    schema_version: Literal[1]
    evidence_status: Literal["hypothetical_only"]
    model_probability_status: Literal["unavailable"]
    decision_status: Literal["human_review_required"]
    currency: str
    horizon_hours: int
    assumptions: dict[str, Any]
    asset_economics: list[dict[str, Any]]
    total_expected_cost_if_all_deferred: str
    schedule: dict[str, Any]
    limitations: list[str]


class SeriesRow(_Strict):
    asset_id: str
    source_row_index: int
    event_time_source: str
    measurements: dict[str, float]


class SeriesPage(_Strict):
    items: list[SeriesRow]
    offset: int
    limit: int
    next_offset: int | None
    time_basis: str
    sensor_units: dict[str, str | None]
    provenance: ArtifactRecord


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _artifact(
    catalog: Catalog, kind: str, artifact_id: str
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    try:
        return catalog.get(kind, artifact_id)
    except CatalogError as exc:
        status = 404 if str(exc) == "unknown artifact" else 409
        raise _error(
            status,
            "artifact_not_found" if status == 404 else "artifact_unavailable",
            "unknown artifact" if status == 404 else "registered artifact failed verification",
        ) from exc


def _time(value: datetime, label: str) -> datetime:
    if value.tzinfo is not None or value.microsecond != 0:
        raise _error(422, "invalid_time", f"{label} must be source-naive with whole seconds")
    return value


def create_app(root: Path, database: Path) -> FastAPI:
    """Create a local API; callers control the trusted artifact root and catalog path."""
    catalog = Catalog(root, database)
    application = FastAPI(
        title="MachineGuard local research API",
        version="1.0.0",
        description="Verified local artifacts and hypothetical scenarios; no calibrated risk.",
    )

    @application.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Any:
        client = request.client.host if request.client else ""
        try:
            allowed = ip_address(client).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "local_only", "message": "loopback clients only"}},
            )
        content_length = request.headers.get("content-length")
        if request.method in {"POST", "PUT", "PATCH"} and content_length is None:
            return JSONResponse(
                status_code=411,
                content={
                    "error": {"code": "length_required", "message": "Content-Length required"}
                },
            )
        if content_length is not None and (
            not content_length.isdecimal() or int(content_length) > 65536
        ):
            return JSONResponse(
                status_code=413,
                content={"error": {"code": "request_too_large", "message": "64 KiB body limit"}},
            )
        return await call_next(request)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(part) for part in item["loc"]) for item in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_request", "fields": fields}},
        )

    @application.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    @application.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "scope": "local_research_only"}

    @application.get("/api/v1/artifacts", response_model=ArtifactPage)
    def artifacts(
        kind: Literal["dataset", "baseline", "rul"] | None = None,
        offset: int = Query(0, ge=0, le=1000000),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        return catalog.list(kind, offset, limit)

    @application.get("/api/v1/assets", response_model=AssetPage)
    def assets(
        offset: int = Query(0, ge=0, le=1000000),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        dataset_page = catalog.list("dataset", offset, limit)
        datasets = dataset_page["items"]
        items = [
            {
                "asset_id": ASSET_ID,
                "dataset_artifact_id": item["artifact_id"],
                "dataset_id": "metropt-3-uci",
                "time_basis": "source_naive_unresolved",
                "source_sha256": item["summary"]["source_sha256"],
            }
            for item in datasets
        ]
        return {"items": items, "offset": offset, "limit": limit, "total": dataset_page["total"]}

    @application.get("/api/v1/datasets/{artifact_id}/quality", response_model=QualityResponse)
    def quality(artifact_id: str) -> dict[str, Any]:
        _, manifest, record = _artifact(catalog, "dataset", artifact_id)
        return {
            "quality": manifest["quality"],
            "provenance": record,
            "time_basis": "source_naive_unresolved",
        }

    @application.get("/api/v1/datasets/{artifact_id}/series", response_model=SeriesPage)
    def series(
        artifact_id: str,
        start: datetime,
        end: datetime,
        sensors: str = Query("tp3", max_length=120),
        offset: int = Query(0, ge=0, le=1000000),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        beginning, ending = _time(start, "start"), _time(end, "end")
        if beginning >= ending or ending - beginning > timedelta(days=7):
            raise _error(422, "invalid_range", "range must be positive and at most seven days")
        names = sensors.split(",")
        if (
            not 1 <= len(names) <= 4
            or len(set(names)) != len(names)
            or any(name not in _SENSORS for name in names)
        ):
            raise _error(422, "invalid_sensors", "select one to four distinct known sensors")
        path, _, record = _artifact(catalog, "dataset", artifact_id)
        items: list[dict[str, Any]] = []
        seen = 0
        try:
            for batch in iter_canonical_batches(path, batch_size=10000):
                columns = batch.select(
                    ["asset_id", "source_row_index", "event_time_source", *names]
                )
                for row in columns.to_pylist():
                    timestamp = row["event_time_source"]
                    if not beginning <= timestamp < ending:
                        continue
                    if seen >= offset:
                        items.append(
                            {
                                "asset_id": row["asset_id"],
                                "source_row_index": row["source_row_index"],
                                "event_time_source": timestamp.isoformat(),
                                "measurements": {name: row[name] for name in names},
                            }
                        )
                        if len(items) > limit:
                            break
                    seen += 1
                if len(items) > limit:
                    break
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise _error(409, "artifact_unavailable", "series verification failed") from exc
        more = len(items) > limit
        return {
            "items": items[:limit],
            "offset": offset,
            "limit": limit,
            "next_offset": offset + limit if more else None,
            "time_basis": "source_naive_unresolved",
            "sensor_units": {name: _SENSORS[name] for name in names},
            "provenance": record,
        }

    @application.get("/api/v1/baselines/{artifact_id}/alerts", response_model=AlertsResponse)
    def alerts(artifact_id: str) -> dict[str, Any]:
        path, _, record = _artifact(catalog, "baseline", artifact_id)
        evaluation = json.loads((path / "evaluation.json").read_text(encoding="utf-8"))
        return {
            "metrics": evaluation["metrics"],
            "reported_windows": evaluation["reported_windows"],
            "metric_caveat": evaluation["metric_caveat"],
            "evidence_status": "descriptive_anomaly_only",
            "provenance": record,
        }

    @application.get("/api/v1/rul/{artifact_id}", response_model=RulResponse)
    def rul(artifact_id: str) -> dict[str, Any]:
        _, report, record = _artifact(catalog, "rul", artifact_id)
        return {
            "dataset_id": "nasa-cmapss-classic",
            "target": report["target"],
            "domain": report["domain"],
            "subsets": {
                name: {"validation": item["validation"], "publisher_test": item["publisher_test"]}
                for name, item in report["subsets"].items()
            },
            "provenance": record,
            "transfer_status": "not_validated_for_metropt_or_industrial_compressors",
        }

    @application.get("/api/v1/assets/{asset_id}/risk", response_model=UnavailableResponse)
    def risk(asset_id: str) -> dict[str, str]:
        if asset_id != ASSET_ID:
            raise _error(404, "asset_not_found", "unknown asset")
        return {
            "status": "unavailable",
            "reason": "MetroPT event evidence does not support calibrated horizon risk",
        }

    @application.get("/api/v1/assets/{asset_id}/rul", response_model=UnavailableResponse)
    def asset_rul(asset_id: str) -> dict[str, str]:
        if asset_id != ASSET_ID:
            raise _error(404, "asset_not_found", "unknown asset")
        return {
            "status": "unavailable",
            "reason": "C-MAPSS RUL is not validated for MetroPT compressors",
        }

    @application.post("/api/v1/scenarios/evaluate", response_model=ScenarioResponse)
    def scenario(payload: ScenarioPayload) -> dict[str, Any]:
        try:
            assumed = Scenario.from_dict(payload.model_dump())
        except ValueError as exc:
            raise _error(422, "invalid_scenario", str(exc)) from exc
        return evaluate_scenario(assumed)

    return application


def _default_app() -> FastAPI:
    profile = os.environ.get("MACHINEGUARD_API_PROFILE", "local")
    if profile == "public_demo":
        from machineguard.api.public import PublicDemoConfig, create_public_app

        return create_public_app(PublicDemoConfig.from_environment())
    if profile != "local":
        raise RuntimeError("unknown MachineGuard API profile")
    root = Path(os.environ.get("MACHINEGUARD_ARTIFACT_ROOT", "data/processed"))
    database = Path(os.environ.get("MACHINEGUARD_CATALOG_DB", "data/processed/api_catalog.sqlite"))
    return create_app(root, database)


app = _default_app()
