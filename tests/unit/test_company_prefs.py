# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The per-user company-exclusion overlay, and the seam it hangs off.

Two claims are being pinned here, and the first one is the whole reason this
ships before there is any way to write an exclusion:

1. **An empty overlay composes the fetch set byte-identically.** Nothing writes
   these documents yet, so every real cycle takes this path. If the empty case
   is not identical, the feature is not inert and the seam was not free.
2. An exclusion subtracts *exactly* one ``(platform, slug)`` pair from the
   fetch set — not the slug on another platform, not a prefix, not the pool.

Plus the property that makes the read safe to do at all: it happens **once per
cycle**, before the fan-out, so a whole cycle runs against one snapshot rather
than a view that changes under it board by board.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml
from google.cloud import firestore

import tools.companies as tc
import tools.company_prefs as prefs
import tools.discovery.pipeline as discovery


# ---------------------------------------------------------------------------
# Fakes: just enough Firestore for users/{uid}/company_prefs
# ---------------------------------------------------------------------------
class _FakeSnap:
    def __init__(self, doc_id: str, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _FakePrefsCollection:
    def __init__(self, db, user_id: str):
        self._db = db
        self._user_id = user_id

    async def stream(self):
        self._db.stream_calls += 1
        for doc_id, data in self._db.docs.get(self._user_id, {}).items():
            yield _FakeSnap(doc_id, data)


class _FakeUserDoc:
    def __init__(self, db, user_id: str):
        self._db = db
        self._user_id = user_id

    def collection(self, name):
        assert name == prefs.COLLECTION, name
        return _FakePrefsCollection(self._db, self._user_id)


class _FakeUsers:
    def __init__(self, db):
        self._db = db

    def document(self, user_id):
        return _FakeUserDoc(self._db, user_id)


class _FakeDB:
    """``docs`` maps user_id -> {doc_id: stored dict}. Counts its streams."""

    def __init__(self, docs: dict | None = None):
        self.docs = docs or {}
        self.stream_calls = 0

    def collection(self, name):
        assert name == "users", name
        return _FakeUsers(self)


def _excluded(platform: str, slug: str, action: str = "block") -> dict:
    """A well-formed overlay document, as the write path will store it."""
    return {
        "platform": platform,
        "slug": slug,
        "state": "excluded",
        "action": action,
        "reason": None,
        "updated_at": "2026-09-03T00:00:00+00:00",
    }


def _overlay(user_id: str, *pairs: tuple[str, str]) -> _FakeDB:
    return _FakeDB(
        {user_id: {prefs.exclusion_key(p, s): _excluded(p, s) for p, s in pairs}}
    )


def _load(db, user_id="u1"):
    return asyncio.run(prefs.load_exclusions(db, user_id))


# ---------------------------------------------------------------------------
# exclusion_key
# ---------------------------------------------------------------------------
def test_the_key_is_platform_colon_slug() -> None:
    assert prefs.exclusion_key("greenhouse", "stripe") == "greenhouse:stripe"


def test_a_key_containing_a_slash_is_refused() -> None:
    """A ``/`` in a document id silently addresses a different subcollection.

    The three ATS platforms cannot produce one (a slug is a single URL path
    segment), but ``google_jobs``/``meta_jobs`` reuse the slug slot as a
    free-text search query, so the type system is not the guarantee here.
    """
    with pytest.raises(ValueError):
        prefs.exclusion_key("google_jobs", "software engineer (backend/infra)")


# ---------------------------------------------------------------------------
# load_exclusions
# ---------------------------------------------------------------------------
def test_an_empty_overlay_is_an_empty_frozenset() -> None:
    loaded = _load(_FakeDB())
    assert loaded == frozenset()
    assert isinstance(loaded, frozenset), "the snapshot must be immutable"


def test_excluded_pairs_come_back() -> None:
    db = _overlay("u1", ("greenhouse", "stripe"), ("lever", "netflix"))
    assert _load(db) == frozenset({("greenhouse", "stripe"), ("lever", "netflix")})


def test_the_read_is_one_stream_not_one_per_document() -> None:
    db = _overlay("u1", ("greenhouse", "a"), ("greenhouse", "b"), ("ashby", "c"))
    _load(db)
    assert db.stream_calls == 1


def test_state_is_what_is_read_not_action() -> None:
    """``action`` is UI vocabulary and audit; ``state`` is what the pipeline acts
    on. A document carrying an action but not the excluded state is not an
    exclusion — otherwise the fetch set is coupled to what the buttons are
    called."""
    doc = _excluded("greenhouse", "stripe")
    doc["state"] = "active"
    db = _FakeDB({"u1": {"greenhouse:stripe": doc}})
    assert _load(db) == frozenset()


def test_a_document_with_no_state_is_ignored() -> None:
    db = _FakeDB({"u1": {"greenhouse:stripe": {"platform": "greenhouse", "slug": "s"}}})
    assert _load(db) == frozenset()


@pytest.mark.parametrize(
    "doc",
    [
        None,  # to_dict() came back empty
        {},  # no fields at all
        {"state": "excluded"},  # partial: no platform, no slug
        {"state": "excluded", "slug": "stripe"},  # no platform
        {"state": "excluded", "platform": "greenhouse"},  # no slug
        {"state": "excluded", "platform": "workday", "slug": "s"},  # unknown platform
        {"state": "excluded", "platform": "greenhouse", "slug": ""},  # empty slug
        {"state": "excluded", "platform": "greenhouse", "slug": 7},  # wrong type
    ],
)
def test_a_malformed_document_is_skipped_not_raised(doc) -> None:
    """One bad overlay row must not take down a crawl."""
    db = _FakeDB({"u1": {"bad": doc, "greenhouse:ok": _excluded("greenhouse", "ok")}})
    assert _load(db) == frozenset({("greenhouse", "ok")})


def test_one_users_overlay_is_not_anothers() -> None:
    db = _overlay("u1", ("greenhouse", "stripe"))
    assert _load(db, "u2") == frozenset()


# ---------------------------------------------------------------------------
# all_active_companies(exclusions=...)
# ---------------------------------------------------------------------------
@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A small, known company pool in place of data/companies."""
    d = tmp_path / "companies"
    d.mkdir()
    (d / "known.yaml").write_text(
        yaml.safe_dump(
            {
                "greenhouse": [{"slug": "stripe"}, {"slug": "figma"}],
                # Same slug, different platform: the pair is the key, not the slug.
                "lever": [{"slug": "stripe"}, {"slug": "netflix"}],
            }
        )
    )
    (d / "unvetted.yaml").write_text(
        yaml.safe_dump({"greenhouse": [{"slug": "newco"}], "ashby": [{"slug": "acme"}]})
    )
    (d / "blocklist.yaml").write_text(yaml.safe_dump({"blocked": []}))
    monkeypatch.setattr(tc, "DATA_DIR", d)
    return d


def test_an_empty_overlay_composes_the_identical_pool(data_dir) -> None:
    """The safety claim of the whole change: inert until something writes."""
    assert tc.all_active_companies(frozenset()) == tc.all_active_companies()


def test_the_default_is_empty_so_existing_callers_are_unchanged(data_dir) -> None:
    assert tc.all_active_companies() == [
        ("greenhouse", "stripe", "known"),
        ("greenhouse", "figma", "known"),
        ("lever", "stripe", "known"),
        ("lever", "netflix", "known"),
        ("greenhouse", "newco", "unvetted"),
        ("ashby", "acme", "unvetted"),
    ]


def test_an_exclusion_removes_exactly_that_pair(data_dir) -> None:
    """Including: the same slug on another platform survives."""
    before = tc.all_active_companies()
    after = tc.all_active_companies(frozenset({("greenhouse", "stripe")}))

    assert ("greenhouse", "stripe", "known") not in after
    assert ("lever", "stripe", "known") in after
    assert [c for c in before if c not in after] == [("greenhouse", "stripe", "known")]


def test_an_exclusion_can_remove_an_unvetted_company(data_dir) -> None:
    after = tc.all_active_companies(frozenset({("greenhouse", "newco")}))
    assert ("greenhouse", "newco", "unvetted") not in after
    assert ("ashby", "acme", "unvetted") in after


def test_an_exclusion_that_matches_nothing_changes_nothing(data_dir) -> None:
    after = tc.all_active_companies(frozenset({("greenhouse", "not-in-the-pool")}))
    assert after == tc.all_active_companies()


def test_excluding_everything_yields_an_empty_fetch_set(data_dir) -> None:
    everything = frozenset((p, s) for p, s, _ in tc.all_active_companies())
    assert tc.all_active_companies(everything) == []


# ---------------------------------------------------------------------------
# run_discovery reads the overlay once, before the fan-out
# ---------------------------------------------------------------------------
def _record_fetches(monkeypatch) -> list[str]:
    fetched: list[str] = []

    async def fetcher(slug: str, user_id: str):
        fetched.append(slug)
        return []

    for platform in tc.PLATFORMS:
        monkeypatch.setitem(discovery.FETCHERS, platform, fetcher)
    return fetched


def test_an_excluded_board_is_never_fetched(data_dir, monkeypatch) -> None:
    """End to end through the real compose: overlay in, board not crawled."""
    fetched = _record_fetches(monkeypatch)
    db = _overlay("u1", ("greenhouse", "stripe"))

    asyncio.run(discovery.run_discovery("u1", db=db))

    assert "stripe" in fetched, "the lever board with the same slug still runs"
    assert sorted(fetched) == ["acme", "figma", "netflix", "newco", "stripe"]


def test_with_an_empty_overlay_every_board_is_still_fetched(
    data_dir, monkeypatch
) -> None:
    fetched = _record_fetches(monkeypatch)

    asyncio.run(discovery.run_discovery("u1", db=_FakeDB()))

    assert sorted(fetched) == sorted(s for _, s, _ in tc.all_active_companies())


def test_the_overlay_is_read_once_per_cycle_not_once_per_board(monkeypatch) -> None:
    """198 boards, one read. A per-board read is 198 reads *and* — the reason
    that matters — lets a mid-cycle write apply to some boards and not others,
    so one cycle would use two different views of the world."""
    companies = [("greenhouse", f"c{i}", "known") for i in range(198)]
    monkeypatch.setattr(
        discovery, "all_active_companies", lambda exclusions=frozenset(): companies
    )
    _record_fetches(monkeypatch)
    db = _FakeDB()

    asyncio.run(discovery.run_discovery("u1", db=db))

    assert db.stream_calls == 1


def test_the_overlay_is_read_before_the_fan_out(monkeypatch) -> None:
    """Not merely once — once *first*. A read interleaved with the fetches is
    still a cycle that saw the world change under it."""
    order: list[str] = []

    class _WatchedDB(_FakeDB):
        def collection(self, name):
            order.append("read")
            return super().collection(name)

    async def fetcher(slug: str, user_id: str):
        order.append("fetch")
        return []

    monkeypatch.setattr(
        discovery,
        "all_active_companies",
        lambda exclusions=frozenset(): [
            ("greenhouse", f"c{i}", "known") for i in range(5)
        ],
    )
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    asyncio.run(discovery.run_discovery("u1", db=_WatchedDB()))

    assert order == ["read"] + ["fetch"] * 5


def test_without_a_db_the_cycle_builds_its_own_client(monkeypatch) -> None:
    """The production callers pass nothing (``run_discovery(user_id)``), so the
    default path has to reach a real client — conftest otherwise refuses one,
    which is exactly what would catch this going unpatched."""
    built = _FakeDB()
    monkeypatch.setattr(discovery, "all_active_companies", lambda exclusions: [])
    monkeypatch.setattr(firestore, "AsyncClient", lambda *a, **k: built)

    asyncio.run(discovery.run_discovery("u1"))

    assert built.stream_calls == 1
