"""Direct public-profile contract tests; all storage is disposable and synthetic."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send

from machineguard.api.app import _default_app, create_app
from machineguard.api.catalog import CatalogError
from machineguard.api.demo import ASSETS, DATA_ORIGIN, DemoCatalog, initialize_demo
from machineguard.api.public import PUBLIC_ORIGIN, PublicDemoConfig, PublicGate, create_public_app


@pytest.fixture(scope="module")
def demo_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("machineguard-public-demo") / "demo"
    initialize_demo(root)
    return root


@pytest.fixture
def remote(demo_root: Path) -> TestClient:
    return TestClient(
        create_public_app(PublicDemoConfig(demo_root, "http://127.0.0.1:8093")),
        base_url=PUBLIC_ORIGIN,
        client=("198.51.100.7", 40000),
    )


def test_profile_must_be_explicit_and_paths_separate(
    monkeypatch: pytest.MonkeyPatch, demo_root: Path, tmp_path: Path
) -> None:
    monkeypatch.delenv("MACHINEGUARD_DEMO_ROOT", raising=False)
    with pytest.raises(ValueError, match="required"):
        PublicDemoConfig.from_environment()
    with pytest.raises(ValueError, match="absolute"):
        PublicDemoConfig(Path("relative"))
    for origin in ("*", "https://example.com", "http://0.0.0.0:8093", "http://127.0.0.1:8093/path"):
        with pytest.raises(ValueError, match="acceptance origin"):
            PublicDemoConfig(demo_root, origin)
    monkeypatch.setenv("MACHINEGUARD_DEMO_ROOT", str(demo_root))
    monkeypatch.setenv("MACHINEGUARD_CATALOG_DB", str(tmp_path / "research.sqlite"))
    with pytest.raises(ValueError, match="research"):
        PublicDemoConfig.from_environment()
    monkeypatch.delenv("MACHINEGUARD_CATALOG_DB")
    monkeypatch.setenv("MACHINEGUARD_API_PROFILE", "public_demo")
    public = _default_app()
    assert public.openapi_url is None
    assert (
        TestClient(public, base_url=PUBLIC_ORIGIN, client=("198.51.100.7", 40000))
        .get("/api/v1/health")
        .status_code
        == 200
    )
    monkeypatch.setenv("MACHINEGUARD_API_PROFILE", "unexpected")
    with pytest.raises(RuntimeError, match="unknown"):
        _default_app()
    local = TestClient(
        create_app(tmp_path / "research", tmp_path / "research.sqlite"),
        client=("198.51.100.7", 40000),
    )
    assert local.get("/api/v1/health").status_code == 403
    assert local.get("/api/v1/health", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403


def test_remote_read_workflow_and_truthful_provenance(remote: TestClient, demo_root: Path) -> None:
    assert remote.get("/api/v1/health").json() == {"status": "ok", "scope": "public_synthetic_demo"}
    assets = remote.get("/api/v1/assets").json()
    assert [item["asset_id"] for item in assets["items"]] == list(ASSETS)
    assert assets["total"] == 2
    artifacts = remote.get("/api/v1/artifacts").json()
    assert len(artifacts["items"]) == 2
    assert {item["summary"]["data_origin"] for item in artifacts["items"]} == {DATA_ORIGIN}
    assert remote.get("/api/v1/artifacts?kind=baseline").json()["total"] == 0
    assert remote.get("/api/v1/artifacts?kind=rul").json()["total"] == 0
    artifact_id = artifacts["items"][0]["artifact_id"]
    quality = remote.get(f"/api/v1/datasets/{artifact_id}/quality")
    assert quality.status_code == 200
    assert quality.json()["quality"]["data_origin"] == DATA_ORIGIN
    path = f"/api/v1/datasets/{artifact_id}/series"
    params = {
        "start": "2020-02-01T00:00:00",
        "end": "2020-02-01T01:00:00",
        "sensors": "tp3,motor_current",
        "limit": 100,
    }
    first = remote.get(path, params=params)
    assert first.status_code == 200
    assert len(first.json()["items"]) == 100
    assert first.json()["next_offset"] == 100
    second = remote.get(path, params={**params, "offset": 100})
    assert second.status_code == 200
    assert second.json()["items"][0]["source_row_index"] == 100
    assert second.json()["time_basis"] == "synthetic_naive"
    assert remote.get(path, params={**params, "limit": 501}).status_code == 422
    assert remote.get(path, params={**params, "sensors": "tp3,bad"}).status_code == 422
    assert remote.get(path, params={**params, "end": "2020-02-09T00:00:00"}).status_code == 422
    assert remote.get(f"/api/v1/assets/{ASSETS[0]}/risk").json()["status"] == "unavailable"
    assert remote.get(f"/api/v1/assets/{ASSETS[0]}/rul").json()["status"] == "unavailable"
    assert remote.get("/api/v1/datasets/aaaaaaaaaaaaaaaaaaaaaaaa/quality").status_code == 404
    assert all(not path.exists() for path in (demo_root / "research.sqlite", demo_root / "models"))


def test_host_origin_forwarding_and_capability_denials(remote: TestClient) -> None:
    assert remote.get("/api/v1/health", headers={"Host": "evil.example"}).status_code == 403
    assert (
        remote.get("/api/v1/health", headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert (
        remote.get(
            "/api/v1/health",
            headers={
                "Forwarded": "host=evil.example;for=127.0.0.1",
                "X-Forwarded-For": "127.0.0.1",
            },
        ).status_code
        == 200
    )
    for path in (
        "/docs",
        "/redoc",
        "/openapi.json",
        "/debug",
        "/admin",
        "/api/v1/ingest",
        "/api/v1/rul/deadbeef",
    ):
        assert remote.get(path).status_code == 403
    assert remote.delete("/api/v1/artifacts").status_code == 403
    assert (
        remote.post("/api/v1/artifacts", headers={"Origin": PUBLIC_ORIGIN}, json={}).status_code
        == 403
    )
    assert remote.get("/api/v1/health", headers={"Host": "127.0.0.1:8093"}).status_code == 200


def test_scenario_body_and_complexity(remote: TestClient) -> None:
    example = json.loads(Path("configs/maintenance_scenario.synthetic.json").read_text())
    url = "/api/v1/scenarios/evaluate"
    headers = {"Origin": PUBLIC_ORIGIN}
    result = remote.post(url, headers=headers, json=example)
    assert result.status_code == 200, result.text
    assert result.json()["evidence_status"] == "hypothetical_only"
    assert remote.post(url, json=example).status_code == 403
    assert (
        remote.post(url, headers={"Origin": "https://evil.example"}, json=example).status_code
        == 403
    )
    assert remote.post(url, headers=headers, content=b"not json").status_code == 415
    assert (
        remote.post(
            url, headers={**headers, "Content-Type": "application/json"}, content=b"{"
        ).status_code
        == 422
    )
    oversized = json.dumps({**example, "assumptions_note": "x" * 66000}).encode()
    assert (
        remote.post(
            url, headers={**headers, "Content-Type": "application/json"}, content=oversized
        ).status_code
        == 413
    )
    assert (
        remote.post(
            url,
            headers={**headers, "Content-Type": "application/json", "Content-Length": "10"},
            content=oversized,
        ).status_code
        == 413
    )

    def chunks():
        yield b" " * 40000
        yield b" " * 30000

    assert (
        remote.post(
            url,
            headers={**headers, "Content-Type": "application/json", "Transfer-Encoding": "chunked"},
            content=chunks(),
        ).status_code
        == 413
    )
    large = copy.deepcopy(example)
    large["assets"] = [copy.deepcopy(example["assets"][0]) for _ in range(4)]
    for index, asset in enumerate(large["assets"]):
        asset["asset_id"] = f"assumed-{index}"
    assert remote.post(url, headers=headers, json=large).status_code == 422
    large = copy.deepcopy(example)
    large["horizon_hours"] = 169
    assert remote.post(url, headers=headers, json=large).status_code == 422


def test_deterministic_bundle_and_tamper_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    first = initialize_demo(root)
    second = initialize_demo(root)
    assert first["artifacts"] == second["artifacts"]
    artifact_id = first["artifacts"][0]["artifact_id"]
    app = create_public_app(PublicDemoConfig(root))
    client = TestClient(app, base_url=PUBLIC_ORIGIN, client=("198.51.100.7", 40000))
    part = root / "artifacts" / artifact_id / "data.parquet"
    with part.open("ab") as stream:
        stream.write(b"tampered")
    assert client.get(f"/api/v1/datasets/{artifact_id}/quality").status_code == 409
    assert client.get("/api/v1/health").status_code == 409
    with pytest.raises(CatalogError):
        DemoCatalog(root)


def test_expensive_request_concurrency_limit(demo_root: Path) -> None:
    async def exercise() -> None:
        entered = 0
        release = asyncio.Event()

        async def slow_app(_scope: Scope, _receive: Receive, send: Send) -> None:
            nonlocal entered
            entered += 1
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        gate = PublicGate(slow_app, PublicDemoConfig(demo_root))
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/datasets/593dfd9f69ab4ad21b115cb1/series",
            "headers": [(b"host", b"machineguard.skrabana.com")],
        }

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def run_one() -> int:
            messages: list[dict[str, object]] = []

            async def send(message: dict[str, object]) -> None:
                messages.append(message)

            await gate(scope, receive, send)  # type: ignore[arg-type]
            return int(messages[0]["status"])

        running = [asyncio.create_task(run_one()) for _ in range(4)]
        await asyncio.sleep(0)
        assert entered == 4
        assert await run_one() == 503
        release.set()
        assert await asyncio.gather(*running) == [200] * 4

    asyncio.run(exercise())
