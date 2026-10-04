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
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from firestore_fakes import _FakeDB as _AllowanceDB

import api.deps as deps
import api.routes.discovery as discovery
from api.deps import verify_user
from tools.discovery import budget as discovery_budget

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
    ``google.auth.default()`` and — the reason it matters here — runs
    ``load_dotenv()``, which would put the developer's ``AUTH_DEV_MODE`` back
    into the process for every test that follows.
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

    async def fake_run_discovery_cycle(user_id, *, trigger="scheduled", score=True):
        started.append(("in_process", user_id, trigger, score))

    async def fake_dispatch_cycle(kind, user_id, *, trigger, score=True):
        started.append(("queued", user_id, trigger, score))
        return True

    monkeypatch.setattr(discovery, "run_discovery_cycle", fake_run_discovery_cycle)
    monkeypatch.setattr(discovery, "dispatch_cycle", fake_dispatch_cycle)
    # The route reads (and, past the refusal, charges) the weekly search
    # allowance. A real fake, not an unlimited stub: a run refused for being
    # local must also leave that counter alone, and it can only be seen to.
    allowance = _AllowanceDB()
    monkeypatch.setattr(discovery, "_async_client", lambda: allowance)

    app = FastAPI()
    app.include_router(discovery.router)
    app.dependency_overrides[verify_user] = lambda: "u1"
    return TestClient(app), started, allowance


@pytest.mark.parametrize("queue_mode", ["0", "1"])
def test_a_local_process_cannot_start_a_live_discovery_run(
    discovery_client, monkeypatch, queue_mode
):
    """Both arms of the QUEUE_MODE branch spend the same money — one on this
    instance, one on the worker — so the refusal has to come before it.

    ``TestClient`` runs ``background_tasks`` synchronously, which is what turned
    "schedule a crawl" into "run a crawl" on 2026-08-23.
    """
    client, started, allowance = discovery_client
    monkeypatch.setenv("QUEUE_MODE", queue_mode)
    monkeypatch.setenv("AUTH_DEV_MODE", "1")

    resp = client.post("/settings/discovery/run")

    assert resp.status_code == 403
    assert discovery.LIVE_RUN_OVERRIDE in resp.json()["detail"]
    assert started == []
    # The refusal is ahead of the weekly allowance too, so a developer's
    # refused click does not burn one of the user's searches.
    assert allowance.store == {}


@pytest.mark.parametrize("queue_mode", ["0", "1"])
def test_the_same_request_is_honoured_from_a_deployed_service(
    discovery_client, monkeypatch, queue_mode
):
    """Positive control: the guard is the dev bypass, not the endpoint."""
    client, started, allowance = discovery_client
    monkeypatch.setenv("QUEUE_MODE", queue_mode)

    assert client.post("/settings/discovery/run").status_code == 200
    assert allowance.budget_state["runs_this_week"] == 1

    where = "queued" if queue_mode == "1" else "in_process"
    # ``score`` is False: the unconfirmed manual click is the *free* verb
    # since the spend-consent seam landed. Pinned here as well as in
    # test_discovery_fanout, because this file is where the shape of what the
    # route dispatches is asserted.
    assert started == [(where, "u1", "manual", False)]


def test_the_override_hands_a_developer_the_run_back(discovery_client, monkeypatch):
    client, started, _allowance = discovery_client
    monkeypatch.setenv("AUTH_DEV_MODE", "1")
    monkeypatch.setenv(discovery.LIVE_RUN_OVERRIDE, "1")

    assert client.post("/settings/discovery/run").status_code == 200
    assert started == [("in_process", "u1", "manual", False)]


def test_the_cycle_itself_refuses_before_it_touches_anything(monkeypatch):
    """The route is not the only way in: the opportunistic tick behind ``GET
    /settings/discovery``, ``cron_tick``'s fan-out and the onboarding kickoff
    all reach ``run_discovery_cycle`` directly. So the guard sits there too, and
    ahead of every write the cycle makes — a refused run costs nothing.

    The one thing it now does reach is the weekly-allowance **refund**: the
    dispatch that sent this run charged a search, and a run refused before it
    crawled anything hands that search back. A credit, not a cost — and only
    for the triggers that were charged.
    """
    monkeypatch.setenv("AUTH_DEV_MODE", "1")

    def explode(*args, **kwargs):
        raise AssertionError("a refused cycle must not reach Firestore")

    async def explode_async(*args, **kwargs):
        raise AssertionError("a refused cycle must not crawl anything")

    allowance = _AllowanceDB(
        state={
            "week": discovery_budget.week_key(datetime.now(UTC), "UTC"),
            "runs_this_week": 3,
        }
    )
    monkeypatch.setattr(discovery, "_client", explode)
    monkeypatch.setattr(discovery, "_async_client", lambda: allowance)
    monkeypatch.setattr(discovery, "_extend_slot", explode)
    monkeypatch.setattr(discovery, "run_discovery", explode_async)

    assert asyncio.run(discovery.run_discovery_cycle("u1", trigger="manual")) is None
    assert allowance.budget_state["runs_this_week"] == 2


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

    # The cycle reads users/{uid} once before it starts, to refuse an account
    # that has been deleted. Answered here with a live document, so ``_client``
    # below stays the refusal it is meant to be.
    monkeypatch.setattr(
        discovery,
        "_user_ref",
        lambda uid: SimpleNamespace(get=lambda: SimpleNamespace(to_dict=lambda: {})),
    )
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
# tests/integration is free, and these keep it that way
# --------------------------------------------------------------------------


def _pytest_config() -> dict:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["tool"]["pytest"]["ini_options"]


def test_billed_tests_are_deselected_by_default():
    """The deselection stays configured even though nothing carries the marker
    today.

    ``pytest tests/integration`` has to be safe to type. It is, currently,
    because no billed test exists — but that is a fact about the tree, not a
    guarantee. This pins the mechanism that makes a *re-added* billed test
    opt-in rather than something you discover from a bill; the companion test
    below pins the absence itself."""
    config = _pytest_config()
    assert any(m.startswith("billed:") for m in config["markers"])
    assert "not billed" in config["addopts"]


def test_integration_suite_costs_nothing():
    """``tests/integration`` contains no billed test at all.

    The two that drove a live model are gone, so the whole directory is free
    to run. The guard fails the moment a paid test is added back, which is
    when that property — and the docs resting on it — stop being true.
    """
    marked = set()
    for path in sorted((REPO_ROOT / "tests" / "integration").glob("test_*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(ast.unparse(d) == "pytest.mark.billed" for d in node.decorator_list):
                marked.add(node.name)
    assert marked == set()


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


# ---------------------------------------------------------------------------
# The register of everything that can bill a third party
#
# The 2026-09-26 spend incident was not a call site anybody had forgotten —
# it was a *chain* nobody had drawn, from a button to a paid Vertex batch four
# frames away. Drawing it required first knowing where the money can leave,
# and that list existed only in someone's head.
#
# So it lives here, as a frozen allowlist walked out of the AST. A new call
# site fails this test, and the fix is to add it to the list *after* deciding
# which consent seam it sits behind. Deliberately a whole-set equality and not
# a subset check: a call site that moves or disappears should also make
# somebody look, because the seam guarding it may now be guarding nothing.
# ---------------------------------------------------------------------------

#: Every module in this repo that can make a third party charge us, and what
#: asks before it does. Paths are repo-relative POSIX.
BILLING_CALL_SITES = {
    # A: online parse (Flash) + score (Pro). Behind the scoring budget, and
    # behind the consent seam on every user-facing route that reaches it.
    "tools/matching/pipeline.py": "gemini",
    # B: the Vertex batch legs. Priced at ingest, hours later — which is why
    # they record a committed estimate at submit (batch_runs._committed).
    "tools/matching/batch.py": "gemini",
    # C: résumé extraction. Still ungated — PUT /profile on first onboarding
    # completion fires it with no button.
    "tools/profile/extract.py": "gemini",
    # D: the tailoring objective rewrite, per approved job.
    "tools/tailoring/objective.py": "gemini",
    # E: Serper, ~$0.30/1k queries. Operator CLI only — no HTTP route reaches
    # it — which is the only reason it carries no seam.
    "tools/discovery/dork.py": "serper",
}

#: Attribute calls that mean "a Gemini model is about to be billed".
_BILLED_GEMINI_CALLS = {"generate_content", "batches"}
_SERPER_HOST = "google.serper.dev"


def _module_bills(path: Path) -> str | None:
    """Which third party this module can charge us with, from its AST alone.

    Source, never import: importing half of these runs ``load_dotenv()`` and
    ``google.auth.default()``, which is exactly what the rest of this file
    goes out of its way to avoid.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # ``client.aio.models.generate_content(...)`` and
            # ``client.aio.batches.create(...)`` — the second is a call on an
            # attribute *of* ``batches``, so both names are checked.
            attrs = {node.func.attr}
            if isinstance(node.func.value, ast.Attribute):
                attrs.add(node.func.value.attr)
            if attrs & _BILLED_GEMINI_CALLS:
                return "gemini"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SERPER_HOST in node.value:
                return "serper"
    return None


def test_every_module_that_can_bill_google_is_registered():
    found = {}
    for path in sorted(REPO_ROOT.glob("**/*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith((".venv/", "tests/", "web/", "node_modules/")):
            continue
        bills = _module_bills(path)
        if bills:
            found[rel] = bills

    assert found == BILLING_CALL_SITES, (
        "the set of modules that can bill a third party changed. Add or "
        "remove the entry in BILLING_CALL_SITES above, and say in the PR "
        "which consent seam the call sits behind."
    )


# --------------------------------------------------------------------------
# /activity is side-effect free, and that is load-bearing
# --------------------------------------------------------------------------


def test_the_activity_route_can_never_schedule_background_work():
    """A status endpoint is the most-polled thing in the app; it must not spend.

    ``GET /jobs/pending`` and ``GET /settings/discovery`` both take a
    ``BackgroundTasks`` and ``add_task(tick_user, …)``. Under QUEUE_MODE that
    tick dispatches a discovery cycle, which chains unconditionally into
    scoring, which on a backlog over ``BATCH_MIN_PENDING`` submits a **paid**
    Vertex batch. That is the 2026-09-26 incident's mechanism, still live on
    those two routes by decision rather than oversight.

    ``/activity`` is polled harder than either, so it may not have the same
    shape. Pinned on the signature rather than by exercising a path: the only
    way FastAPI hands a route a scheduler is through that parameter, so its
    absence is the property itself, and no fake can accidentally satisfy it.
    """
    import inspect

    import api.routes.activity as activity

    sig = inspect.signature(activity.get_activity)
    annotations = {str(p.annotation) for p in sig.parameters.values()}
    assert not any("BackgroundTasks" in a for a in annotations), sig
    assert "background_tasks" not in sig.parameters

    # And nothing in the module reaches a scheduler or a write by another name.
    source = Path(activity.__file__).read_text()
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    forbidden = called & {"add_task", "set", "update", "delete", "create_task"}
    assert not forbidden, f"/activity is supposed to be read-only: {forbidden}"

    # By identifier, not by substring: the module *documents* why it must not
    # tick, so the words appear in its docstring. Names and imports are what
    # would make it actually possible.
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    }
    assert "tick_user" not in names | imported
    assert "BackgroundTasks" not in names | imported


def test_the_activity_route_is_authenticated_like_every_other_read():
    import inspect

    import api.routes.activity as activity

    default = inspect.signature(activity.get_activity).parameters["user_id"].default
    assert getattr(default, "dependency", None) is verify_user
