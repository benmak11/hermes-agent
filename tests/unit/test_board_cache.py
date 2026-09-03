# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The shared board cache: one fetch per board per TTL, for every user at once.

Two users' discovery cycles each fetched 13,083 jobs with byte-identical
per-platform splits, because the boards do not know who is asking.
``tools.ats.board_cache`` stores each board's normalized ``Job`` list once and
re-stamps it per user.

Two properties carry the whole design and are pinned hardest here:

* **``jd_raw`` survives byte-for-byte.** ``jd_cache.jd_hash`` is a bare
  ``sha256`` of the text with no normalization, so one changed byte misses the
  cache on every one of the ~7,266 existing parses and this module would cost
  money rather than save it.
* **The payload is user-independent.** Anything user-shaped that leaked in
  would be served to the next user who reads the blob.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_company_prefs import _FakeDB

import tools.discovery.pipeline as discovery
from models.job import Job
from tools.ats import board_cache
from tools.matching.jd_cache import jd_hash

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _ForbiddenCall(BaseException):
    """Not an ``Exception``: ``board_cache`` swallows every ``Exception`` by
    design (a broken cache must not break a cycle), so an ``AssertionError``
    from a fake would be absorbed into a plain cache miss and the test would
    fail with the wrong story. This propagates."""


class _FakeBlob:
    def __init__(self, world, name):
        self.world = world
        self.name = name

    def download_as_bytes(self):
        self.world.calls["download"] += 1
        if self.name not in self.world.store:
            raise FileNotFoundError(f"404 {self.name}")  # stands in for NotFound
        if self.world.read_error:
            raise RuntimeError("GCS is having a day")
        return self.world.store[self.name]

    def upload_from_string(self, data, content_type=None):
        self.world.calls["upload"] += 1
        if self.world.write_error:
            raise RuntimeError("GCS is having a day")
        assert content_type == "application/json", content_type
        self.world.store[self.name] = data

    # The trap this module exists to avoid: checking freshness on metadata that
    # a separate download then discards. None of these may ever be reached.
    def exists(self, *a, **kw):
        raise _ForbiddenCall("board_cache called blob.exists()")

    def reload(self, *a, **kw):
        raise _ForbiddenCall("board_cache called blob.reload()")

    @property
    def updated(self):
        raise _ForbiddenCall("board_cache read blob.updated (always None here)")


class _FakeBucket:
    def __init__(self, world, name):
        self.world = world
        self.name = name

    def blob(self, name):
        return _FakeBlob(self.world, name)


class _FakeStorageClient:
    def __init__(self, world):
        self.world = world

    def bucket(self, name):
        assert name == "test-bucket", name
        self.world.calls["client"] += 1
        return _FakeBucket(self.world, name)


@pytest.fixture
def gcs(monkeypatch):
    """A fake bucket, with the cache OFF. Tests that want it on set the TTL."""
    import google.cloud.storage as gcs_mod

    world = SimpleNamespace(
        store={},
        calls={"download": 0, "upload": 0, "client": 0},
        read_error=False,
        write_error=False,
    )
    monkeypatch.setenv("RESUME_BUCKET", "test-bucket")
    monkeypatch.delenv("BOARD_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.setattr(gcs_mod, "Client", lambda *a, **kw: _FakeStorageClient(world))
    board_cache.reset_client()
    yield world
    board_cache.reset_client()


@pytest.fixture
def warm(gcs, monkeypatch):
    """The same fake bucket with the cache switched on for an hour."""
    monkeypatch.setenv("BOARD_CACHE_TTL_SECONDS", "3600")
    return gcs


# A JD carrying every byte a normalizer would be tempted to touch: CRLF, a bare
# CR, the U+2028/U+2029 separators that already shattered a JSONL join in this
# project, NBSP, a zero-width space, a NUL, an astral-plane emoji, doubled and
# trailing spaces, and a literal backslash-n that is NOT a newline.
NASTY_JD = (
    "  Senior\u00a0Engineer \r\n\r\n"
    "\tBuild things\u2028and ship them\u2029 — \u201cfast\u201d.  \n"
    "Literal not-a-newline: \\n\u200b\r"
    "Emoji: \U0001f680\U0001f9ea  \x00 trailing spaces   "
)


def _job(**over) -> Job:
    base = {
        "id": "abc123",
        "user_id": "u1",
        "source": "greenhouse",
        "source_id": "777",
        "company": "acme",
        "title": "Senior Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/777",
        "location": "Remote - US",
        "jd_raw": NASTY_JD,
        "discovered_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(over)
    return Job(**base)


def _store(platform="greenhouse", slug="acme", jobs=None):
    asyncio.run(board_cache.store_jobs(platform, slug, jobs or [_job()]))


def _load(platform="greenhouse", slug="acme", user_id="u2"):
    return asyncio.run(board_cache.load_jobs(platform, slug, user_id))


# --------------------------------------------------------------------------
# Shipped inert
# --------------------------------------------------------------------------


def test_the_cache_is_off_unless_the_ttl_says_otherwise(gcs):
    """Ships inert, the way GEO_GATE_ENFORCE and QUEUE_MODE did: not merely
    "reads miss", but *no GCS client is ever built*."""
    assert board_cache.ttl_seconds() == 0
    assert board_cache.enabled() is False

    _store()
    assert _load() is None

    assert gcs.store == {}
    assert gcs.calls == {"download": 0, "upload": 0, "client": 0}


@pytest.mark.parametrize("raw", ["0", "-5", "nonsense", "  ", "3.5"])
def test_an_unusable_ttl_leaves_the_cache_off(gcs, monkeypatch, raw):
    """Every way of failing to configure this lands on "off", never on a
    surprise default."""
    monkeypatch.setenv("BOARD_CACHE_TTL_SECONDS", raw)
    assert board_cache.enabled() is False


def test_the_ttl_switches_it_on(warm):
    assert board_cache.ttl_seconds() == 3600
    assert board_cache.enabled() is True
    _store()
    assert warm.calls["upload"] == 1


# --------------------------------------------------------------------------
# The byte-identity constraint
# --------------------------------------------------------------------------


def test_jd_raw_survives_the_round_trip_byte_for_byte(warm):
    """The constraint the whole PR hangs on.

    ``jd_cache`` keys on ``sha256(jd_raw.encode())`` and says so: "exact-match
    by design — no normalization". Strip, re-wrap, re-run ``html_to_text``, or
    let a serializer fold a line ending, and every cached board becomes a
    guaranteed miss against ~7,266 existing parses — a cache that spends money
    instead of saving it.
    """
    original = _job()
    _store(jobs=[original])
    loaded = _load()

    assert loaded is not None
    # Byte identity, asserted on bytes — not "the parsed structures are equal".
    assert loaded[0].jd_raw.encode("utf-8") == original.jd_raw.encode("utf-8")
    # And the consequence that actually matters downstream.
    assert jd_hash(loaded[0].jd_raw) == jd_hash(original.jd_raw)


def test_the_stored_bytes_are_ascii_so_a_lone_surrogate_cannot_break_a_write(warm):
    """Board JSON comes off the wire, and a ``\\udXXX`` escape in a posting
    yields a string ``.encode("utf-8")`` refuses outright. Escaping non-ASCII
    on the way out sidesteps that and still round-trips the text exactly."""
    lonely = _job(jd_raw="before \ud800 after")
    _store(jobs=[lonely])

    blob = gcs_blob(warm)
    blob.decode("ascii")  # would raise if anything non-ASCII were written
    loaded = _load()
    assert loaded is not None
    assert loaded[0].jd_raw == "before \ud800 after"


def gcs_blob(world) -> bytes:
    (only,) = world.store.values()
    return only


# --------------------------------------------------------------------------
# User-independence
# --------------------------------------------------------------------------


def test_nothing_user_shaped_is_written_to_a_shared_blob(warm):
    _store(jobs=[_job(user_id="user-alpha")])

    written = json.loads(gcs_blob(warm))
    (stored_job,) = written["jobs"]

    assert "user_id" not in stored_job
    assert "discovered_at" not in stored_job
    assert "user-alpha" not in gcs_blob(warm).decode("ascii")


def test_every_other_field_is_the_posting_and_survives_unchanged(warm):
    """The field-by-field claim, asserted rather than argued: strip the two
    per-user fields and what is left must come back identical."""
    original = _job(user_id="user-alpha")
    _store(jobs=[original])
    (loaded,) = _load(user_id="user-beta")

    before = original.model_dump(mode="json")
    after = loaded.model_dump(mode="json")
    for field in board_cache.PER_USER_FIELDS:
        before.pop(field)
        after.pop(field)
    assert after == before
    # Including the id, which is sha256("<platform>:<source_id>")[:16] — already
    # user-independent, but this is what makes that a checked fact.
    assert loaded.id == original.id


def test_the_reader_is_stamped_as_the_owner_of_what_it_read(warm):
    before = datetime.now(UTC)
    _store(
        jobs=[
            _job(user_id="user-alpha", discovered_at=datetime(2020, 1, 1, tzinfo=UTC))
        ]
    )
    (loaded,) = _load(user_id="user-beta")

    assert loaded.user_id == "user-beta"
    # discovered_at is re-stamped *now*, not inherited from whoever fetched it:
    # it means "when this user first saw the posting", and persist_new_jobs
    # writes it onto that user's job doc.
    assert loaded.discovered_at >= before


# --------------------------------------------------------------------------
# Freshness, and the one-download rule
# --------------------------------------------------------------------------


def test_one_download_decides_both_freshness_and_content(warm):
    """``exists()`` → ``reload()`` → ``download_as_bytes()`` is three round
    trips that check freshness against metadata they then discard and then
    download a possibly *different* object. Worse, ``download_as_bytes``
    populates no ``updated`` header at all, so the check would never pass. The
    fake raises a BaseException on all three; one download is the only shape
    that survives."""
    _store()
    warm.calls["download"] = 0

    assert _load() is not None
    assert warm.calls["download"] == 1


def test_an_entry_older_than_the_ttl_is_a_miss(warm, monkeypatch):
    _store()
    monkeypatch.setenv("BOARD_CACHE_TTL_SECONDS", "1")
    assert board_cache._is_fresh(json.loads(gcs_blob(warm)), 1) is True

    stale = json.loads(gcs_blob(warm))
    stale["fetched_at"] = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    _rewrite(warm, stale)

    assert _load() is None


def test_a_timestamp_from_the_future_is_a_miss_not_maximum_freshness(warm):
    """Clock skew or a corrupt payload; the alternative reading would pin a
    stale board in place for as long as the skew lasts."""
    _store()
    payload = json.loads(gcs_blob(warm))
    payload["fetched_at"] = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    _rewrite(warm, payload)

    assert _load() is None


@pytest.mark.parametrize("stamp", [None, "", "not-a-date", 1234567890])
def test_an_unreadable_timestamp_is_a_miss(warm, stamp):
    _store()
    payload = json.loads(gcs_blob(warm))
    payload["fetched_at"] = stamp
    _rewrite(warm, payload)

    assert _load() is None


def test_a_naive_timestamp_is_a_miss(warm):
    """No tzinfo means no defensible comparison — guessing UTC here would be
    wrong by up to a day and would fail with a TypeError anyway."""
    _store()
    payload = json.loads(gcs_blob(warm))
    payload["fetched_at"] = datetime.now(UTC).isoformat().removesuffix("+00:00")
    _rewrite(warm, payload)

    assert _load() is None


def _rewrite(world, payload: dict) -> None:
    (name,) = world.store
    world.store[name] = json.dumps(payload).encode("ascii")


# --------------------------------------------------------------------------
# Failing open
# --------------------------------------------------------------------------


def test_a_cold_cache_is_a_quiet_miss(warm):
    assert _load() is None


def test_a_read_error_fails_open(warm):
    _store()
    warm.read_error = True
    assert _load() is None  # not an exception


def test_a_write_error_is_swallowed(warm):
    warm.write_error = True
    _store()  # must not raise
    assert warm.store == {}


def test_corrupt_bytes_are_a_miss(warm):
    _store()
    (name,) = warm.store
    warm.store[name] = b"{not json at all"
    assert _load() is None


@pytest.mark.parametrize("payload", [b"[1, 2, 3]", b'"a string"', b"null"])
def test_a_payload_that_is_not_an_object_is_a_miss(warm, payload):
    _store()
    (name,) = warm.store
    warm.store[name] = payload
    assert _load() is None


def test_a_payload_from_another_version_is_a_miss(warm):
    _store()
    payload = json.loads(gcs_blob(warm))
    payload["version"] = board_cache.PAYLOAD_VERSION + 1
    _rewrite(warm, payload)

    assert _load() is None


def test_schema_drift_is_a_miss_and_the_next_fetch_overwrites_it(warm):
    """Same self-healing contract as ``jd_cache``: a stored job that no longer
    validates reads as a miss rather than raising or half-loading."""
    _store()
    payload = json.loads(gcs_blob(warm))
    del payload["jobs"][0]["title"]  # required field
    _rewrite(warm, payload)

    assert _load() is None

    _store()  # a re-fetch heals it
    assert _load() is not None


def _corrupt_second_job(payload: dict, how: str) -> dict:
    if how == "not-a-dict":
        payload["jobs"][1] = "not a job at all"
    else:
        del payload["jobs"][1]["title"]  # required field
    return payload


@pytest.mark.parametrize("how", ["not-a-dict", "fails-validation"])
def test_one_bad_job_invalidates_the_board_rather_than_shrinking_it(warm, how):
    """Skipping the bad entry and returning the survivors would present a board
    that quietly lost a posting — and nothing downstream can tell that from a
    role actually being taken down. Both corruption shapes have to reject the
    whole blob, not just the one that happens to raise ``ValidationError``."""
    _store(jobs=[_job(id="a", source_id="1"), _job(id="b", source_id="2")])
    _rewrite(warm, _corrupt_second_job(json.loads(gcs_blob(warm)), how))

    assert _load() is None


# --------------------------------------------------------------------------
# Object naming
# --------------------------------------------------------------------------


def test_the_prefix_cannot_collide_with_a_user_reset():
    """``cli/reset_user.py`` deletes exactly ``users/{uid}/``. A demo reset for
    one user must not evict the cache every other user shares."""
    path = board_cache.blob_path("greenhouse", "acme")
    assert path == "board_cache/greenhouse/acme.json"
    assert not path.startswith("users/")


def test_a_search_query_slug_is_quoted_into_one_flat_object_name():
    """``google_jobs``/``meta_jobs`` reuse the slug slot as a search query, so
    the "slug" really is ``software engineer``."""
    assert (
        board_cache.blob_path("google_jobs", "software engineer")
        == "board_cache/google_jobs/software%20engineer.json"
    )
    # Two different queries must never fold onto one blob.
    assert board_cache.blob_path("meta_jobs", "a/b") != board_cache.blob_path(
        "meta_jobs", "a%2Fb"
    )
    assert "/" not in board_cache.blob_path("meta_jobs", "a/b").split("/")[-1]


def test_platforms_do_not_share_a_namespace(warm):
    _store(platform="greenhouse", slug="acme", jobs=[_job(source_id="gh")])
    _store(platform="lever", slug="acme", jobs=[_job(source="lever", source_id="lv")])

    assert len(warm.store) == 2
    assert _load(platform="greenhouse", slug="acme")[0].source_id == "gh"
    assert _load(platform="lever", slug="acme")[0].source_id == "lv"


# --------------------------------------------------------------------------
# The fan-out
# --------------------------------------------------------------------------


def _one_board(platform="greenhouse", slug="acme"):
    return lambda exclusions=frozenset(): [(platform, slug, "known")]


def _counting_fetcher(calls: list, jobs=None):
    async def fetcher(slug, user_id):
        calls.append(user_id)
        return [_job(user_id=user_id)] if jobs is None else list(jobs)

    return fetcher


def test_the_second_users_cycle_does_not_refetch_the_board(warm, monkeypatch):
    """The measured problem, in miniature: two users, one board, byte-identical
    results, two crawls. After this it is one crawl."""
    calls: list[str] = []
    monkeypatch.setattr(discovery, "all_active_companies", _one_board())
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _counting_fetcher(calls))

    first = asyncio.run(discovery.run_discovery("u1", db=_FakeDB()))
    second = asyncio.run(discovery.run_discovery("u2", db=_FakeDB()))

    assert calls == ["u1"], "the board was fetched again for the second user"
    assert [j.user_id for j in second["jobs"]] == ["u2"]
    assert second["jobs"][0].jd_raw == first["jobs"][0].jd_raw
    assert second["jobs"][0].id == first["jobs"][0].id


def test_provenance_is_still_stamped_on_a_cached_board(warm, monkeypatch):
    """``discovered_via`` is applied by the pipeline *after* the fetch, so it
    must survive the cache being wired in front of it — the "first time seeing
    this company" badge reads it."""
    monkeypatch.setattr(
        discovery,
        "all_active_companies",
        lambda exclusions=frozenset(): [("greenhouse", "acme", "unvetted")],
    )
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _counting_fetcher([]))

    asyncio.run(discovery.run_discovery("u1", db=_FakeDB()))
    summary = asyncio.run(discovery.run_discovery("u2", db=_FakeDB()))

    assert summary["boards_cached"] == 1
    assert [j.discovered_via for j in summary["jobs"]] == ["unvetted"]


def test_the_summary_counts_hits_and_fetches(warm, monkeypatch):
    """``boards_cached``/``boards_fetched`` are what make the flag flip
    measurable; a hit count alone cannot tell "off" from "cold"."""
    monkeypatch.setattr(discovery, "all_active_companies", _one_board())
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _counting_fetcher([]))

    cold = asyncio.run(discovery.run_discovery("u1", db=_FakeDB()))
    hot = asyncio.run(discovery.run_discovery("u2", db=_FakeDB()))

    assert (cold["boards_cached"], cold["boards_fetched"]) == (0, 1)
    assert (hot["boards_cached"], hot["boards_fetched"]) == (1, 0)


def test_a_failing_board_counts_as_fetched_not_cached(warm, monkeypatch):
    async def fetcher(slug, user_id):
        raise RuntimeError("board exploded")

    monkeypatch.setattr(discovery, "all_active_companies", _one_board())
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", fetcher)

    summary = asyncio.run(discovery.run_discovery("u1", db=_FakeDB()))

    assert len(summary["failures"]) == 1
    assert (summary["boards_cached"], summary["boards_fetched"]) == (0, 1)


def test_an_empty_board_is_never_cached(warm, monkeypatch):
    """A 404, a 429 that spent its retries, and a genuinely empty board all
    arrive here as ``[]`` — ``fetch_board_json`` absorbs them identically. So
    caching an empty result would take one transient board-side failure and
    serve it to every user for a whole TTL. That is the one way this module
    could lose jobs, and it is why the empty case is refused."""
    calls: list[str] = []
    monkeypatch.setattr(discovery, "all_active_companies", _one_board())
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _counting_fetcher(calls, []))

    asyncio.run(discovery.run_discovery("u1", db=_FakeDB()))
    second = asyncio.run(discovery.run_discovery("u2", db=_FakeDB()))

    assert warm.store == {}
    assert calls == ["u1", "u2"], "an empty board was cached and hid the re-probe"
    assert second["boards_fetched"] == 1


def test_with_the_cache_off_the_fan_out_touches_no_storage(gcs, monkeypatch):
    """The shipped default has to be the old code path, not a fast path through
    new code."""
    calls: list[str] = []
    monkeypatch.setattr(discovery, "all_active_companies", _one_board())
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _counting_fetcher(calls))

    asyncio.run(discovery.run_discovery("u1", db=_FakeDB()))
    summary = asyncio.run(discovery.run_discovery("u2", db=_FakeDB()))

    assert calls == ["u1", "u2"]
    assert gcs.calls == {"download": 0, "upload": 0, "client": 0}
    assert (summary["boards_cached"], summary["boards_fetched"]) == (0, 1)


def test_a_broken_bucket_does_not_break_a_cycle(warm, monkeypatch):
    """The contract that matters most in production: a cache that breaks a
    cycle is strictly worse than no cache."""
    warm.read_error = warm.write_error = True
    calls: list[str] = []
    monkeypatch.setattr(discovery, "all_active_companies", _one_board())
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _counting_fetcher(calls))

    summary = asyncio.run(discovery.run_discovery("u1", db=_FakeDB()))

    assert summary["failures"] == []
    assert len(summary["jobs"]) == 1
    assert (summary["boards_cached"], summary["boards_fetched"]) == (0, 1)


def test_two_cycles_missing_the_same_board_both_write_without_raising(
    warm, monkeypatch
):
    """No lock and no ``if_generation_match``, deliberately. Both writers win,
    the payload is identical, and neither raises — which is exactly what
    happens today with no cache at all. A generation precondition would instead
    make the loser raise inside a discovery cycle."""
    monkeypatch.setattr(discovery, "all_active_companies", _one_board())
    monkeypatch.setitem(discovery.FETCHERS, "greenhouse", _counting_fetcher([]))

    async def both():
        return await asyncio.gather(
            discovery.run_discovery("u1", db=_FakeDB()),
            discovery.run_discovery("u2", db=_FakeDB()),
        )

    first, second = asyncio.run(both())

    assert first["failures"] == second["failures"] == []
    assert len(warm.store) == 1
    assert _load() is not None
