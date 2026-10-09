# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``/admin/boards`` through the real admin gate, against a fake Firestore and
company YAML in a temp ``DATA_DIR``."""

from __future__ import annotations

import pytest
import structlog
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from firestore_fakes import FakeStoreDB

import api.deps as deps
import api.routes.admin as admin
import tools.companies as tc

ADMIN = "admin-uid"
AUTH = {"Authorization": "Bearer tok"}


def _patch_decoded(monkeypatch, decoded: dict) -> None:
    monkeypatch.setattr(deps, "_ensure_firebase", lambda: None)
    import firebase_admin.auth as fb_auth_module

    monkeypatch.setattr(fb_auth_module, "verify_id_token", lambda token: dict(decoded))


def _as_admin(monkeypatch) -> None:
    _patch_decoded(
        monkeypatch,
        {"uid": ADMIN, "email": "admin@example.com", "email_verified": True},
    )


def _record(platform: str, slug: str, state: str = "ok", **over) -> dict:
    rec = {
        "platform": platform,
        "slug": slug,
        "state": state,
        "last_outcome": "ok" if state == "ok" else "not_found",
        "last_status": 200 if state == "ok" else 404,
        "last_ok_at": "2026-10-01T00:00:00+00:00",
        "failing_since": None if state == "ok" else "2026-10-03T00:00:00+00:00",
        "not_found_days": 0,
        "last_not_found_day": None,
        "updated_at": "2026-10-05T00:00:00+00:00",
    }
    rec.update(over)
    return rec


def _records(*recs: dict) -> dict:
    return {f"{r['platform']}:{r['slug']}": r for r in recs}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "companies"
    d.mkdir()
    (d / "known.yaml").write_text(
        yaml.safe_dump(
            {
                "greenhouse": [
                    {"slug": "acme", "name": "Acme"},
                    {"slug": "pausedco", "name": "Paused Co", "paused": True},
                    {"slug": "neverco", "name": "Never Co"},
                ],
                "lever": [{"slug": "Beta", "name": "Beta"}],
                "google_jobs": [{"slug": "engineer remote"}],
            }
        )
    )
    (d / "unvetted.yaml").write_text(
        yaml.safe_dump(
            {
                "ashby": [{"slug": "gamma"}, {"slug": "unseen"}],
            }
        )
    )
    (d / "blocklist.yaml").write_text(
        yaml.safe_dump(
            {
                "blocked": [
                    {
                        "platform": "greenhouse",
                        "slug": "spamco",
                        "blocked_at": "2026-05-01",
                        "reason": "ghost jobs",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(tc, "DATA_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _env(monkeypatch, data_dir):
    monkeypatch.setenv("ADMIN_UIDS", ADMIN)
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    monkeypatch.delenv("AUTH_DEV_USER", raising=False)
    monkeypatch.setattr(deps, "_allowlist_cache", {})


def _use(monkeypatch, records: dict) -> FakeStoreDB:
    db = FakeStoreDB({"board_health": records})
    monkeypatch.setattr(admin, "_client", lambda: db)
    return db


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(admin.router)
    return TestClient(app)


def test_boards_is_hidden_from_a_non_admin_like_accounts(monkeypatch, client):
    """Same gate as ``/accounts``: a plain 404, never a 403 that reveals the
    route."""
    _use(monkeypatch, _records(_record("greenhouse", "acme")))
    _patch_decoded(
        monkeypatch, {"uid": "u1", "email": "u1@example.com", "email_verified": True}
    )
    resp = client.get("/admin/boards", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}
    assert client.get("/admin/boards").status_code == 404


def test_boards_sorts_failing_first_then_by_404_days(monkeypatch, client):
    _use(
        monkeypatch,
        _records(
            _record("ashby", "gamma"),
            _record("greenhouse", "acme", "failing", not_found_days=1),
            _record("lever", "Beta", "failing", not_found_days=4),
            _record("greenhouse", "pausedco", "failing", not_found_days=1),
            _record(
                "ashby", "zeta", "failing", last_outcome="timeout", not_found_days=0
            ),
            _record("greenhouse", "aaa"),
        ),
    )
    _as_admin(monkeypatch)
    resp = client.get("/admin/boards", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    order = [(r["platform"], r["slug"]) for r in resp.json()["boards"]]
    assert order == [
        ("lever", "Beta"),
        ("greenhouse", "acme"),
        ("greenhouse", "pausedco"),
        ("ashby", "zeta"),
        ("ashby", "gamma"),
        ("greenhouse", "aaa"),
    ]


def test_boards_joins_the_company_yaml(monkeypatch, client):
    _use(
        monkeypatch,
        _records(
            _record("greenhouse", "acme"),
            _record("greenhouse", "pausedco", "failing", not_found_days=2),
            _record("lever", "beta"),  # case differs from the YAML spelling
            _record("ashby", "gamma"),
            _record("greenhouse", "spamco", "failing"),
            _record("greenhouse", "orphan"),
        ),
    )
    _as_admin(monkeypatch)
    rows = {
        (r["platform"], r["slug"]): r
        for r in client.get("/admin/boards", headers=AUTH).json()["boards"]
    }
    pick = ("name", "list", "paused", "blocklisted")

    def joined(key):
        return {k: rows[key][k] for k in pick}

    assert joined(("greenhouse", "acme")) == {
        "name": "Acme",
        "list": "known",
        "paused": False,
        "blocklisted": False,
    }
    assert joined(("greenhouse", "pausedco")) == {
        "name": "Paused Co",
        "list": "known",
        "paused": True,
        "blocklisted": False,
    }
    assert joined(("lever", "beta"))["list"] == "known"
    assert joined(("ashby", "gamma")) == {
        "name": None,
        "list": "unvetted",
        "paused": False,
        "blocklisted": False,
    }
    assert joined(("greenhouse", "spamco")) == {
        "name": None,
        "list": "none",
        "paused": False,
        "blocklisted": True,
    }
    assert joined(("greenhouse", "orphan"))["list"] == "none"
    full = rows[("greenhouse", "pausedco")]
    assert full["state"] == "failing"
    assert full["last_outcome"] == "not_found"
    assert full["last_status"] == 404
    assert full["not_found_days"] == 2
    assert full["failing_since"] == "2026-10-03T00:00:00+00:00"
    assert full["last_ok_at"] == "2026-10-01T00:00:00+00:00"
    assert full["updated_at"] == "2026-10-05T00:00:00+00:00"
    assert "last_not_found_day" not in full


def test_totals_count_states_404s_and_never_checked_boards(monkeypatch, client):
    """Active tracked boards are acme, neverco, Beta (known) and gamma, unseen
    (unvetted). Paused, blocklisted and search-query slugs never count as
    unchecked."""
    _use(
        monkeypatch,
        _records(
            _record("greenhouse", "acme", "failing", not_found_days=3),
            _record("lever", "beta"),
            _record("ashby", "gamma", "failing", last_outcome="timeout"),
            _record("greenhouse", "orphan"),
        ),
    )
    _as_admin(monkeypatch)
    totals = client.get("/admin/boards", headers=AUTH).json()["totals"]
    assert totals == {
        "total": 4,
        "ok": 2,
        "failing": 2,
        "quarantined": 0,
        "dead": 0,
        "moved": 0,
        "not_found": 1,
        "never_checked": 2,  # neverco, unseen
    }


def test_an_empty_collection_counts_every_active_board_as_never_checked(
    monkeypatch, client
):
    _use(monkeypatch, {})
    _as_admin(monkeypatch)
    resp = client.get("/admin/boards", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {
        "totals": {
            "total": 0,
            "ok": 0,
            "failing": 0,
            "quarantined": 0,
            "dead": 0,
            "moved": 0,
            "not_found": 0,
            "never_checked": 5,
        },
        "boards": [],
    }


class _BrokenDB:
    def collection(self, name):
        return self

    async def stream(self):
        raise RuntimeError("firestore down for u1@example.com")
        yield  # pragma: no cover


def test_a_firestore_error_is_the_accounts_503_shape(monkeypatch, client):
    monkeypatch.setattr(admin, "_client", lambda: _BrokenDB())
    _as_admin(monkeypatch)
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/admin/boards", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "could not read board_health"}
    assert resp.headers["cache-control"] == "no-store"
    [failed] = [e for e in logs if e["event"] == "admin.boards_failed"]
    assert failed["source"] == "board_health"
    assert failed["error_type"] == "RuntimeError"
    assert "@" not in str(failed)


def test_an_unreadable_company_list_is_a_503_too(monkeypatch, client, data_dir):
    _use(monkeypatch, _records(_record("greenhouse", "acme")))
    (data_dir / "known.yaml").write_text("greenhouse: [{slug: acme, bogus: [}")
    _as_admin(monkeypatch)
    resp = client.get("/admin/boards", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "could not read company lists"}


def test_boards_never_writes_firestore_or_the_yaml(monkeypatch, client, data_dir):
    records = _records(
        _record("greenhouse", "acme", "failing", not_found_days=2),
        _record("lever", "Beta"),
    )
    db = _use(monkeypatch, records)
    before_db = {k: dict(v) for k, v in records.items()}
    before_yaml = {p.name: p.read_bytes() for p in data_dir.iterdir()}
    _as_admin(monkeypatch)
    assert client.get("/admin/boards", headers=AUTH).status_code == 200
    assert db.writes == []
    assert db.deletes == []
    assert db.transactions == []
    assert db.data["board_health"] == before_db
    assert {p.name: p.read_bytes() for p in data_dir.iterdir()} == before_yaml


def test_boards_logs_one_count_only_audit_line(monkeypatch, client):
    _use(monkeypatch, _records(_record("greenhouse", "acme", "failing")))
    _as_admin(monkeypatch)
    with structlog.testing.capture_logs() as logs:
        assert client.get("/admin/boards", headers=AUTH).status_code == 200
    [viewed] = [e for e in logs if e["event"] == "admin.boards_viewed"]
    assert {k: viewed[k] for k in ("rows", "failing", "never_checked")} == {
        "rows": 1,
        "failing": 1,
        "never_checked": 4,
    }
    assert "acme" not in str(viewed)


# ------------------------------------------- reroute / quarantine / prune


def _probed_records() -> dict:
    return _records(
        _record("greenhouse", "acme"),
        _record(
            "greenhouse",
            "pausedco",
            "moved",
            not_found_days=2,
            resolves_to={"platform": "lever", "slug": "pausedco"},
            candidates=[
                {
                    "platform": "lever",
                    "slug": "pausedco",
                    "name": "Paused Co",
                    "job_count": 4,
                    "probed_at": "2026-10-05T00:00:00+00:00",
                }
            ],
        ),
        _record("lever", "Beta", "dead", not_found_days=3),
        _record(
            "ashby",
            "gamma",
            "quarantined",
            not_found_days=2,
            candidates=[
                {
                    "platform": "greenhouse",
                    "slug": "gamma",
                    "name": "Gamma Labs",
                    "job_count": 7,
                    "probed_at": "2026-10-05T00:00:00+00:00",
                },
                {
                    "platform": "lever",
                    "slug": "gamma",
                    "name": None,
                    "job_count": 1,
                    "probed_at": "2026-10-05T00:00:00+00:00",
                },
            ],
        ),
        _record("greenhouse", "neverco", "failing", not_found_days=1),
        _record("ashby", "unseen", "quarantined", not_found_days=5, candidates=[]),
    )


def test_new_states_sort_failing_quarantined_dead_moved_ok(monkeypatch, client):
    _use(monkeypatch, _probed_records())
    _as_admin(monkeypatch)
    rows = client.get("/admin/boards", headers=AUTH).json()["boards"]
    assert [(r["state"], r["slug"]) for r in rows] == [
        ("failing", "neverco"),
        ("quarantined", "unseen"),
        ("quarantined", "gamma"),
        ("dead", "Beta"),
        ("moved", "pausedco"),
        ("ok", "acme"),
    ]


def test_an_unknown_state_sorts_after_ok(monkeypatch, client):
    _use(
        monkeypatch,
        _records(_record("greenhouse", "acme", "brand_new"), _record("lever", "Beta")),
    )
    _as_admin(monkeypatch)
    rows = client.get("/admin/boards", headers=AUTH).json()["boards"]
    assert [r["state"] for r in rows] == ["ok", "brand_new"]


def test_totals_count_the_new_states(monkeypatch, client):
    _use(monkeypatch, _probed_records())
    _as_admin(monkeypatch)
    totals = client.get("/admin/boards", headers=AUTH).json()["totals"]
    assert {
        k: totals[k] for k in ("total", "ok", "failing", "quarantined", "dead", "moved")
    } == {
        "total": 6,
        "ok": 1,
        "failing": 1,
        "quarantined": 2,
        "dead": 1,
        "moved": 1,
    }


def test_moved_rows_carry_the_target_and_quarantined_rows_the_candidates(
    monkeypatch, client
):
    _use(monkeypatch, _probed_records())
    _as_admin(monkeypatch)
    rows = {
        r["slug"]: r for r in client.get("/admin/boards", headers=AUTH).json()["boards"]
    }
    assert rows["pausedco"]["resolves_to"] == {"platform": "lever", "slug": "pausedco"}
    assert rows["gamma"]["resolves_to"] is None
    assert rows["gamma"]["candidates"] == [
        {
            "platform": "greenhouse",
            "slug": "gamma",
            "name": "Gamma Labs",
            "job_count": 7,
        },
        {"platform": "lever", "slug": "gamma", "name": None, "job_count": 1},
    ]
    assert rows["acme"]["candidates"] == []
    assert rows["acme"]["resolves_to"] is None
