# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The guards that stop a local process behaving like — or against — production.

Three unrelated-looking things with one shape in common: something that is
harmless on a laptop and expensive or exposed on the live project.

- ``pytest tests/integration`` used to make real, billed Gemini calls.
- ``GET /docs`` answered 200 unauthenticated on the deployed API.
- ``POST /settings/discovery/run``, driven from a ``TestClient`` with the dev
  auth bypass on, ran a real 198-board crawl against production. That is not
  hypothetical: 2026-08-23, ~110s, 8,469 junk jobs, ~$0.50-1.00.

The unit suite is hermetic about the environment variable all of this turns on
— see ``conftest.no_dev_bypass``, which exists because importing any ``cli/``
module pulls the developer's real ``.env`` into the pytest process — so every
test here sets what it means to test.
"""

from __future__ import annotations

import ast
import asyncio
import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.deps as deps
import api.routes.discovery as discovery
from api.deps import verify_user

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# The signal itself
# --------------------------------------------------------------------------


def test_dev_mode_is_off_unless_the_bypass_is_explicitly_on(monkeypatch):
    """One project, and it is production — so "am I pointed at prod?" cannot
    tell a laptop from Cloud Run. ``AUTH_DEV_MODE`` can: Terraform does not set
    it, so a deployed revision never has it."""
    assert deps.dev_mode() is False  # conftest cleared it
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    assert deps.dev_mode() is True
    monkeypatch.setenv("AUTH_DEV_MODE", "0")
    assert deps.dev_mode() is False


# --------------------------------------------------------------------------
# /docs and /openapi.json
# --------------------------------------------------------------------------


def _gateway_app_call() -> ast.Call:
    """The bare ``FastAPI(...)`` construction in ``api/main.py``.

    Read from source rather than imported: importing ``api.main`` calls
    ``google.auth.default()``, mounts ADK and — the reason it matters here —
    runs ``load_dotenv()``, which would put the developer's ``AUTH_DEV_MODE``
    back into the process for every test that follows.
    """
    tree = ast.parse((REPO_ROOT / "api" / "main.py").read_text())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FastAPI"
    )


@pytest.mark.parametrize("kwarg", ["docs_url", "openapi_url", "redoc_url"])
def test_the_gateway_publishes_its_shape_only_to_a_developer(kwarg):
    """Nothing behind ``/docs`` is data or a billable model, so this is
    reconnaissance aid rather than a vulnerability — and correspondingly cheap
    to close. Each URL must be conditioned on ``dev_mode()`` with ``None`` as
    the production answer; a bare string would republish it."""
    keywords = {kw.arg: kw.value for kw in _gateway_app_call().keywords}
    assert kwarg in keywords, f"api.main builds FastAPI without {kwarg}"
    value = keywords[kwarg]
    assert isinstance(value, ast.IfExp), ast.unparse(value)
    assert ast.unparse(value.test) == "dev_mode()", ast.unparse(value)
    assert value.orelse.value is None, ast.unparse(value)


# --------------------------------------------------------------------------
# The live-fire guard
# --------------------------------------------------------------------------


def test_live_runs_are_refused_only_from_a_local_process(monkeypatch):
    assert discovery.live_runs_refused() is False  # a deployed service
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    assert discovery.live_runs_refused() is True
    monkeypatch.setenv(discovery.LIVE_RUN_OVERRIDE, "1")
    assert discovery.live_runs_refused() is False  # asked for by name


@pytest.fixture
def discovery_client(monkeypatch):
    """``POST /settings/discovery/run`` with both ways out of it recorded."""
    started: list[tuple] = []

    async def fake_run_discovery_cycle(user_id, *, trigger="scheduled"):
        started.append(("in_process", user_id, trigger))

    async def fake_dispatch_cycle(kind, user_id, *, trigger):
        started.append(("queued", user_id, trigger))
        return True

    monkeypatch.setattr(discovery, "run_discovery_cycle", fake_run_discovery_cycle)
    monkeypatch.setattr(discovery, "dispatch_cycle", fake_dispatch_cycle)

    app = FastAPI()
    app.include_router(discovery.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app), started


@pytest.mark.parametrize("queue_mode", ["0", "1"])
def test_a_local_process_cannot_start_a_live_discovery_run(
    discovery_client, monkeypatch, queue_mode
):
    """Both arms of the QUEUE_MODE branch spend the same money — one on this
    instance, one on the worker — so the refusal has to come before it.

    ``TestClient`` runs ``background_tasks`` synchronously, which is what turned
    "schedule a crawl" into "run a crawl" on 2026-08-23.
    """
    client, started = discovery_client
    monkeypatch.setenv("QUEUE_MODE", queue_mode)
    monkeypatch.setenv("AUTH_DEV_MODE", "1")

    resp = client.post("/settings/discovery/run")

    assert resp.status_code == 403
    assert discovery.LIVE_RUN_OVERRIDE in resp.json()["detail"]
    assert started == []


@pytest.mark.parametrize("queue_mode", ["0", "1"])
def test_the_same_request_is_honoured_from_a_deployed_service(
    discovery_client, monkeypatch, queue_mode
):
    """Positive control: the guard is the dev bypass, not the endpoint."""
    client, started = discovery_client
    monkeypatch.setenv("QUEUE_MODE", queue_mode)

    assert client.post("/settings/discovery/run").status_code == 200

    where = "queued" if queue_mode == "1" else "in_process"
    assert started == [(where, "u1", "manual")]


def test_the_override_hands_a_developer_the_run_back(discovery_client, monkeypatch):
    client, started = discovery_client
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    monkeypatch.setenv(discovery.LIVE_RUN_OVERRIDE, "1")

    assert client.post("/settings/discovery/run").status_code == 200
    assert started == [("in_process", "u1", "manual")]


def test_the_cycle_itself_refuses_before_it_touches_anything(monkeypatch):
    """The route is not the only way in: the opportunistic tick behind ``GET
    /settings/discovery``, ``cron_tick``'s fan-out and the onboarding kickoff
    all reach ``run_discovery_cycle`` directly. So the guard sits there too, and
    ahead of the first Firestore write — a refused run costs nothing at all."""
    monkeypatch.setenv("AUTH_DEV_MODE", "1")

    def explode(*args, **kwargs):
        raise AssertionError("a refused cycle must not reach Firestore")

    async def explode_async(*args, **kwargs):
        raise AssertionError("a refused cycle must not crawl anything")

    monkeypatch.setattr(discovery, "_client", explode)
    monkeypatch.setattr(discovery, "_extend_slot", explode)
    monkeypatch.setattr(discovery, "run_discovery", explode_async)

    assert asyncio.run(discovery.run_discovery_cycle("u1", trigger="manual")) is None


def _no_firestore(*args, **kwargs):
    raise AssertionError(
        "the unit suite must not build a real Firestore client — patch the "
        "seam this test reaches through"
    )


def test_the_cycle_runs_when_nothing_is_bypassing_auth(monkeypatch):
    """Positive control for the test above — the same fakes, minus the flag.

    ``persist_run_cost`` has to be one of those fakes. ``run_discovery`` raising
    is caught by the cycle's ``except``, and the ``finally`` then flushes the
    ledger — with the *real* Firestore client, against the real project, under
    a user id (``u1``) that only exists in this file. Unpatched, this test wrote
    one production document on every run of the unit suite; 39 of them had
    accumulated under ``users/u1/runs`` before anyone looked. A "free, offline"
    suite has to be checked, not assumed.
    """
    reached: list[str] = []
    flushed: list[str] = []

    async def fake_run_discovery(user_id):
        reached.append(user_id)
        raise RuntimeError("far enough")

    async def fake_persist_run_cost(db, user_id, run_id, **kw):
        flushed.append(user_id)

    monkeypatch.setattr(discovery, "_extend_slot", lambda *a, **kw: True)
    monkeypatch.setattr(discovery, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(discovery, "_release_slot", lambda *a, **kw: True)
    monkeypatch.setattr(discovery, "run_cost_snapshot", lambda run_id: {"cost_usd": 0})
    monkeypatch.setattr(discovery, "persist_run_cost", fake_persist_run_cost)
    monkeypatch.setattr(discovery, "_client", _no_firestore)

    asyncio.run(discovery.run_discovery_cycle("u1", trigger="manual"))

    assert reached == ["u1"]
    assert flushed == ["u1"]


# --------------------------------------------------------------------------
# tests/integration bills real Gemini calls
# --------------------------------------------------------------------------


def _pytest_config() -> dict:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["tool"]["pytest"]["ini_options"]


def test_billed_tests_are_deselected_by_default():
    """``pytest tests/integration`` has to be safe to type. It costs ~$0.01-0.02
    a run otherwise, and a throttled one surfaces as ``429 RESOURCE_EXHAUSTED``
    — which reads like a broken test and is really production quota."""
    config = _pytest_config()
    assert any(m.startswith("billed:") for m in config["markers"])
    assert "not billed" in config["addopts"]


def test_every_unmocked_integration_test_carries_the_marker():
    """The two that reach a live model, pinned by name.

    Neither is mocked: one builds a real ``Runner`` around the real
    ``root_agent``, the other posts to ``/run_sse`` on a real server. Nothing
    can detect a *new* billed test automatically, so this at least stops these
    two losing their marker quietly.
    """
    marked = set()
    for path in sorted((REPO_ROOT / "tests" / "integration").glob("test_*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(ast.unparse(d) == "pytest.mark.billed" for d in node.decorator_list):
                marked.add(node.name)
    assert marked == {"test_agent_stream", "test_chat_stream"}


# ---------------------------------------------------------------------------
# The liveness sweep, same guard as discovery.
#
# The sweep buys no LLM calls, which is exactly why it was easy to leave
# unguarded — but it writes `user_decision: dismissed` onto real jobs and moves
# real applications to `posting_removed`. A laptop pointed at production should
# not be able to retire a user's queue by accident.
# ---------------------------------------------------------------------------


@pytest.fixture
def sweep_client(monkeypatch):
    """``POST /settings/discovery/sweep`` with both ways out of it recorded."""
    started: list[tuple] = []

    async def fake_run_sweep_cycle(user_id, *, trigger="scheduled"):
        started.append(("in_process", user_id, trigger))

    async def fake_dispatch_cycle(kind, user_id, *, trigger):
        started.append((kind, user_id, trigger))
        return True

    monkeypatch.setattr(discovery, "run_sweep_cycle", fake_run_sweep_cycle)
    monkeypatch.setattr(discovery, "dispatch_cycle", fake_dispatch_cycle)

    app = FastAPI()
    app.include_router(discovery.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app), started


@pytest.mark.parametrize("queue_mode", ["0", "1"])
def test_a_local_process_cannot_start_a_live_sweep(
    sweep_client, monkeypatch, queue_mode
):
    """Refused ahead of the QUEUE_MODE branch, like discovery: enqueueing from a
    laptop hands the same production writes to the real worker."""
    client, started = sweep_client
    monkeypatch.setenv("QUEUE_MODE", queue_mode)
    monkeypatch.setenv("AUTH_DEV_MODE", "1")

    resp = client.post("/settings/discovery/sweep")

    assert resp.status_code == 403
    assert discovery.LIVE_RUN_OVERRIDE in resp.json()["detail"]
    assert started == []


@pytest.mark.parametrize("queue_mode", ["0", "1"])
def test_the_same_sweep_is_honoured_from_a_deployed_service(
    sweep_client, monkeypatch, queue_mode
):
    client, started = sweep_client
    monkeypatch.setenv("QUEUE_MODE", queue_mode)
    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)

    assert client.post("/settings/discovery/sweep").status_code == 200
    assert len(started) == 1


def test_the_sweep_cycle_itself_refuses_before_it_touches_anything(monkeypatch):
    """``cron_tick``'s fan-out reaches ``run_sweep_cycle`` without going through
    the route, so the guard sits there too — ahead of ``_extend_slot``, so a
    refused sweep leaves no lease behind either."""
    monkeypatch.setenv("AUTH_DEV_MODE", "1")

    def explode(*args, **kwargs):
        raise AssertionError("a refused sweep must not reach Firestore")

    async def explode_async(*args, **kwargs):
        raise AssertionError("a refused sweep must not probe any posting")

    monkeypatch.setattr(discovery, "_client", explode)
    monkeypatch.setattr(discovery, "_extend_slot", explode)
    monkeypatch.setattr(discovery, "sweep_postings", explode_async)

    assert asyncio.run(discovery.run_sweep_cycle("u1", trigger="manual")) is None
