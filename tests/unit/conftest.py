# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared fixtures for the unit suite."""

import pytest
from google.cloud import firestore

import api.deps as api_deps
import api.routes.account as routes_account
import api.routes.applications as routes_applications
import api.routes.companies as routes_companies
import api.routes.discovery as routes_discovery
import api.routes.jobs as routes_jobs
import api.routes.profile as routes_profile
from tools import genai_client
from tools.matching import budget


class _RealFirestoreInTests(BaseException):
    """Not an ``Exception``, so broad ``except Exception`` handlers in the code
    under test cannot swallow the one signal that says a test escaped to the
    live project."""


@pytest.fixture(autouse=True)
def no_dev_bypass(monkeypatch):
    """Keep the developer's ``.env`` out of the unit suite.

    Several ``cli/`` modules call ``load_dotenv()`` **at import time**, and this
    suite imports three of them (``unwedge_submitting``, ``geo_resurrect``,
    ``reap_applications``). So merely collecting these tests loads the real
    ``.env`` — including ``AUTH_DEV_MODE=1`` — into the pytest process, and
    every test after that point runs believing it is a developer at a keyboard
    with the production project configured.

    Nothing depended on that, and one thing now refuses to work under it:
    ``api.routes.discovery.live_runs_refused``. Rather than teach that guard to
    recognise pytest — a guard with an "unless you are testing" clause is not a
    guard — the leak is closed here. Unit tests are hermetic: no dev bypass, no
    ambient permission to drive a live run.
    """
    monkeypatch.delenv("AUTH_DEV_MODE", raising=False)
    monkeypatch.delenv("ALLOW_LIVE_RUNS", raising=False)


@pytest.fixture(autouse=True)
def reset_genai_client():
    """Never let one test's Vertex client leak into the next.

    ``tools.genai_client`` memoises a ``genai.Client`` per event loop so a
    backlog run stops building one per call. A memo and a monkeypatched
    constructor are a bad pair: whichever test runs first wins, and every test
    after it gets that client no matter what it patched — which is precisely how
    ``no_production_firestore`` below spent weeks catching nothing while 39 real
    documents piled up. Clearing before *and* after keeps the memo from being
    load-bearing in either direction.
    """
    genai_client.reset_vertex_client()
    yield
    genai_client.reset_vertex_client()


@pytest.fixture
def unlimited_budget(monkeypatch):
    """Grant every scorer whatever it asks for, without touching Firestore.

    The per-user scoring budget (``tools.matching.budget``) fronts all three
    scoring entry points, so tests *about scoring* have to get past it to
    still be testing scoring. Opt in here; the gate itself is pinned in
    ``test_scoring_budget.py``, which uses the real (pure) implementation.
    """
    granted: list[int] = []

    async def fake_reserve(db, user_id, wanted=None, *, cycle_id=None, limits=None):
        limits = limits or budget.Limits.from_env()
        n = limits.per_cycle if wanted is None else wanted
        granted.append(n)
        return budget.Reservation(
            granted=n,
            capped=False,
            remaining_cycle=limits.per_cycle - n,
            remaining_day=limits.per_day - n,
            cycle_id=cycle_id,
        )

    async def fake_release(db, user_id, unused, *, cycle_id):
        pass

    monkeypatch.setattr(budget, "reserve", fake_reserve)
    monkeypatch.setattr(budget, "release", fake_release)
    return granted


@pytest.fixture(autouse=True)
def no_production_firestore(monkeypatch, request):
    """The unit suite must never build a real Firestore client.

    Found 2026-08-30, by looking: ``users/u1/runs`` held **39 real production
    documents**, one per suite run, written by a test whose fakes stopped one
    seam short of the cost flush. The suite is supposed to be free and offline;
    nothing was checking that it actually was, and a leak this quiet only shows
    up if you go and look at the database.

    Tests that legitimately want a client fake one; this refuses the real
    constructor, so the next leak fails loudly instead of writing.
    """
    if request.node.get_closest_marker("allow_firestore"):
        return

    def _refuse(*args, **kwargs):
        # BaseException, not Exception, and that is the whole trick: the code
        # this guards is telemetry, and telemetry deliberately swallows every
        # Exception so it can never fail a pipeline (see
        # tools.run_costs.persist_run_cost). An AssertionError here is caught
        # by that handler — the write is still prevented, but the suite goes
        # green and the leak stays invisible, which is exactly how 39
        # documents accumulated. This propagates through the broad handler.
        raise _RealFirestoreInTests(
            "tests/unit built a real firestore.Client — patch the seam under "
            "test instead (see conftest.no_production_firestore)"
        )

    monkeypatch.setattr(firestore, "Client", _refuse)
    monkeypatch.setattr(firestore, "AsyncClient", _refuse)

    # Patching the constructor is not enough on its own: every route module
    # memoises its client in a module-level ``_db``, so a client built once
    # (by an earlier test, or an import) is handed out forever and the
    # constructor above is never reached again. Clearing the cache per test is
    # what makes the refusal actually bite — this guard was written without it
    # and silently caught nothing.
    for mod in (
        api_deps,
        routes_account,
        routes_discovery,
        routes_applications,
        routes_companies,
        routes_jobs,
        routes_profile,
    ):
        monkeypatch.setattr(mod, "_db", None, raising=False)
    # ``api.routes.discovery`` memoises a *second* client — an async one, used
    # only by the allowlist check in ``cron_tick`` — beside its usual sync
    # ``_db``. Same leak shape ``_db`` above exists to close: a client built by
    # an earlier test would otherwise be handed out to every test after it.
    monkeypatch.setattr(routes_discovery, "_adb", None, raising=False)
