"""Opt-in, read-oriented API for an isolated synthetic public demonstration."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from machineguard.api.app import (
    _SENSORS,
    ArtifactPage,
    AssetPage,
    QualityResponse,
    ScenarioPayload,
    ScenarioResponse,
    SeriesPage,
    UnavailableResponse,
    _error,
    _time,
)
from machineguard.api.catalog import CatalogError
from machineguard.api.demo import ASSETS, DATASET_ID, DemoCatalog
from machineguard.optimization.contracts import Scenario
from machineguard.optimization.scenarios import evaluate_scenario

PUBLIC_ORIGIN = "https://machineguard.skrabana.com"
MAX_BODY_BYTES = 65_536
MAX_CONCURRENT_WORK = 4
_DATASET_ROUTE = re.compile(r"^/api/v1/datasets/[0-9a-f]{24}/(?:quality|series)$")
_ASSET_ROUTE = re.compile(r"^/api/v1/assets/synthetic-compressor-[ab]/(?:risk|rul)$")


@dataclass(frozen=True)
class PublicDemoConfig:
    root: Path
    acceptance_origin: str | None = None

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or self.root.is_symlink():
            raise ValueError("public demo root must be an absolute, non-symlink path")
        if self.acceptance_origin is not None:
            parsed = urlsplit(self.acceptance_origin)
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or parsed.port is None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
                or self.acceptance_origin != f"http://127.0.0.1:{parsed.port}"
            ):
                raise ValueError("acceptance origin must be an exact 127.0.0.1 HTTP origin")

    @classmethod
    def from_environment(cls) -> PublicDemoConfig:
        root = os.environ.get("MACHINEGUARD_DEMO_ROOT")
        if not root:
            raise ValueError("MACHINEGUARD_DEMO_ROOT is required for public_demo")
        if os.environ.get("MACHINEGUARD_ARTIFACT_ROOT") or os.environ.get(
            "MACHINEGUARD_CATALOG_DB"
        ):
            raise ValueError("research artifact and catalog paths cannot be set in public_demo")
        return cls(Path(root), os.environ.get("MACHINEGUARD_DEMO_ACCEPTANCE_ORIGIN"))


def _reply(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class PublicGate:
    """Validate route, host, origin and bounded body before FastAPI dispatch."""

    def __init__(self, app: ASGIApp, config: PublicDemoConfig) -> None:
        self.app = app
        self.origins = {PUBLIC_ORIGIN}
        if config.acceptance_origin:
            self.origins.add(config.acceptance_origin)
        self.hosts = {urlsplit(origin).netloc for origin in self.origins}
        self.work = asyncio.Semaphore(MAX_CONCURRENT_WORK)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = [(key.lower(), value.decode("latin-1")) for key, value in scope["headers"]]
        host_values = [value for key, value in headers if key == b"host"]
        origin_values = [value for key, value in headers if key == b"origin"]
        host = host_values[0] if len(host_values) == 1 else ""
        if host not in self.hosts:
            await _reply(403, "invalid_host", "host is not approved")(scope, receive, send)
            return
        if origin_values and (len(origin_values) != 1 or origin_values[0] not in self.origins):
            await _reply(403, "invalid_origin", "origin is not approved")(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]
        get_allowed = path in {"/api/v1/health", "/api/v1/assets", "/api/v1/artifacts"} or bool(
            _DATASET_ROUTE.fullmatch(path) or _ASSET_ROUTE.fullmatch(path)
        )
        post_allowed = path == "/api/v1/scenarios/evaluate"
        if (method != "GET" or not get_allowed) and (method != "POST" or not post_allowed):
            await _reply(403, "capability_denied", "endpoint or method is not public")(
                scope, receive, send
            )
            return
        if method == "POST" and (len(origin_values) != 1 or origin_values[0] not in self.origins):
            await _reply(403, "origin_required", "approved Origin required for scenario POST")(
                scope, receive, send
            )
            return
        lengths = [value for key, value in headers if key == b"content-length"]
        if lengths and (len(lengths) != 1 or not lengths[0].isdecimal()):
            await _reply(400, "invalid_length", "invalid Content-Length")(scope, receive, send)
            return
        if lengths and int(lengths[0]) > MAX_BODY_BYTES:
            await _reply(413, "request_too_large", "64 KiB body limit")(scope, receive, send)
            return
        if method == "POST":
            if not any(
                key == b"content-type" and value.split(";", 1)[0].strip() == "application/json"
                for key, value in headers
            ):
                await _reply(415, "json_required", "application/json required")(
                    scope, receive, send
                )
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > MAX_BODY_BYTES:
                    await _reply(413, "request_too_large", "64 KiB body limit")(
                        scope, receive, send
                    )
                    return
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            if lengths and int(lengths[0]) != len(body):
                await _reply(400, "invalid_length", "Content-Length mismatch")(scope, receive, send)
                return
            delivered = False

            async def replay() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            receive = replay
        expensive = method == "POST" or path.endswith("/series") or path.endswith("/quality")
        if expensive and self.work.locked():
            await _reply(503, "busy", "public demo request capacity reached")(scope, receive, send)
            return
        if expensive:
            await self.work.acquire()
        try:
            await self.app(scope, receive, send)
        finally:
            if expensive:
                self.work.release()


def create_public_app(config: PublicDemoConfig) -> FastAPI:
    catalog = DemoCatalog(config.root)
    application = FastAPI(
        title="MachineGuard synthetic public demo",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        debug=False,
    )
    application.add_middleware(PublicGate, config=config)

    @application.exception_handler(RequestValidationError)
    async def invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(part) for part in item["loc"]) for item in exc.errors()]
        return _reply(422, "invalid_request", ", ".join(fields))

    @application.exception_handler(CatalogError)
    async def corrupt(_request: Request, exc: CatalogError) -> JSONResponse:
        if str(exc) == "unknown artifact":
            return _reply(404, "artifact_not_found", "unknown artifact")
        return _reply(409, "artifact_unavailable", "public demo artifact failed verification")

    @application.get("/api/v1/health")
    def health() -> dict[str, str]:
        catalog.list(None, 0, 2)
        for item in catalog.list("dataset", 0, 2)["items"]:
            catalog.get("dataset", item["artifact_id"])
        return {"status": "ok", "scope": "public_synthetic_demo"}

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
        page = catalog.list("dataset", offset, limit)
        return {
            "items": [
                {
                    "asset_id": item["summary"]["asset_id"],
                    "dataset_artifact_id": item["artifact_id"],
                    "dataset_id": DATASET_ID,
                    "time_basis": "synthetic_naive",
                    "source_sha256": item["summary"]["source_sha256"],
                }
                for item in page["items"]
            ],
            "offset": offset,
            "limit": limit,
            "total": page["total"],
        }

    @application.get("/api/v1/datasets/{artifact_id}/quality", response_model=QualityResponse)
    def quality(artifact_id: str) -> dict[str, Any]:
        _, manifest, record = catalog.get("dataset", artifact_id)
        return {
            "quality": manifest["quality"],
            "provenance": record,
            "time_basis": "synthetic_naive",
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
        _, _, record = catalog.get("dataset", artifact_id)
        items, next_offset = catalog.series(artifact_id, beginning, ending, names, offset, limit)
        return {
            "items": items,
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset,
            "time_basis": "synthetic_naive",
            "sensor_units": {name: _SENSORS[name] for name in names},
            "provenance": record,
        }

    @application.get("/api/v1/assets/{asset_id}/risk", response_model=UnavailableResponse)
    def risk(asset_id: str) -> dict[str, str]:
        if asset_id not in ASSETS:
            raise _error(404, "asset_not_found", "unknown asset")
        return {
            "status": "unavailable",
            "reason": "Synthetic demo has no observed failures or calibrated risk model",
        }

    @application.get("/api/v1/assets/{asset_id}/rul", response_model=UnavailableResponse)
    def asset_rul(asset_id: str) -> dict[str, str]:
        if asset_id not in ASSETS:
            raise _error(404, "asset_not_found", "unknown asset")
        return {
            "status": "unavailable",
            "reason": "Synthetic demo has no supported remaining useful life estimate",
        }

    @application.post("/api/v1/scenarios/evaluate", response_model=ScenarioResponse)
    def scenario(payload: ScenarioPayload) -> dict[str, Any]:
        if (
            len(payload.assets) > 3
            or len(payload.slots) > 2
            or payload.horizon_hours > 168
            or any(slot.crew_capacity > 2 for slot in payload.slots)
        ):
            raise _error(
                422,
                "scenario_too_complex",
                "public demo supports at most 3 assets, 2 slots, crew capacity 2, and 168 hours",
            )
        try:
            assumed = Scenario.from_dict(payload.model_dump())
        except ValueError as exc:
            raise _error(422, "invalid_scenario", str(exc)) from exc
        return evaluate_scenario(assumed)

    return application
