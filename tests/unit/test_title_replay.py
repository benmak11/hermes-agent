# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The title replay is an instrument, so these tests aim at the two ways an
instrument lies: counting a loss as a saving, and being unable to see a loss
at all.

The ship bar for the widened map is **zero false drops** in the match-carrying
corpus. A replay that cannot produce a non-zero false-drop count is worth
nothing, so the first test here forces one.
"""

from __future__ import annotations

import pytest

from cli.title_replay import TitleReplay, load_preferences, replay_user
from models.profile import JobPreferences
from tools.matching.score import DISCARD_AT_OR_BELOW

PREFS = JobPreferences(
    target_role_families=["engineering"],
    target_titles=["Senior Software Engineer"],
    target_seniorities=["senior"],
)


def test_a_drop_pro_scored_well_is_a_false_drop():
    r = TitleReplay(user_id="u1")
    r.record("Playable Ads Editor", PREFS, 78.0)
    assert len(r.false_drops) == 1
    assert r.false_drops[0] == (78.0, "marketing", "Playable Ads Editor")
    assert r.true_drops == 0
    assert r.by_family == {"marketing": 1}
    assert r.scored == 1


def test_a_drop_pro_already_rejected_is_a_true_drop():
    r = TitleReplay(user_id="u1")
    r.record("Playable Ads Editor", PREFS, float(DISCARD_AT_OR_BELOW))
    assert r.false_drops == []
    assert r.true_drops == 1


def test_the_threshold_is_exclusive_at_the_boundary():
    """At the threshold Pro discarded it; one point above, Pro kept it."""
    r = TitleReplay(user_id="u1")
    r.record("Playable Ads Editor", PREFS, float(DISCARD_AT_OR_BELOW))
    r.record("Amazon PPC Specialist", PREFS, float(DISCARD_AT_OR_BELOW) + 1)
    assert r.true_drops == 1
    assert [f[2] for f in r.false_drops] == ["Amazon PPC Specialist"]


def test_an_unscored_drop_is_neither():
    """The 8,882-job backlog carries no match; it can only ever count."""
    r = TitleReplay(user_id="u1")
    r.record("Playable Ads Editor", PREFS, None)
    assert r.false_drops == []
    assert r.true_drops == 0
    assert r.scored == 0
    assert sum(r.by_family.values()) == 1


def test_a_job_the_narrow_map_already_dropped_is_not_a_new_loss():
    r = TitleReplay(user_id="u1")
    r.record("Account Executive", PREFS, 90.0)  # sales under both maps
    assert r.narrow["drop"] == 1 and r.wide["drop"] == 1
    assert r.by_family == {}
    assert r.false_drops == []


def test_kept_titles_move_nothing():
    r = TitleReplay(user_id="u1")
    r.record("Senior Software Engineer", PREFS, 88.0)  # explicit target title
    r.record("Staff Backend Engineer", PREFS, 71.0)  # in-family
    r.record("Data Engineer", PREFS, 64.0)  # ambiguous, kept
    assert r.narrow == r.wide
    assert r.by_family == {}
    assert r.false_drops == []
    assert r.narrow["target-title"] == 1
    assert r.narrow["in-family"] == 1
    assert r.narrow["unclassified"] == 1


def test_the_counterfactual_shows_the_map_firing():
    """Corpus B's whole job: today unclassified, under the candidate dropped."""
    r = TitleReplay(user_id="u1")
    for title in ("Technical Project Manager", "Amazon PPC Analyst"):
        r.record(title, PREFS, None)
    assert r.narrow["unclassified"] == 2
    assert r.wide["unclassified"] == 0
    assert r.wide["drop"] == 2
    assert dict(r.by_family) == {"product": 1, "marketing": 1}


def test_without_preferences_nothing_can_drop():
    r = TitleReplay(user_id="u1")
    r.record("Playable Ads Editor", None, None)
    assert r.narrow["no-filter"] == 1
    assert r.by_family == {}


def test_merge_is_additive_across_users():
    a, b = TitleReplay(user_id="a"), TitleReplay(user_id="b")
    a.record("Playable Ads Editor", PREFS, 78.0)
    b.record("Technical Project Manager", PREFS, None)
    b.record_tombstone("Amazon PPC Specialist", PREFS)
    a.merge(b)
    assert a.n == 2
    assert len(a.false_drops) == 1
    assert dict(a.by_family) == {"marketing": 1, "product": 1}
    assert (a.tombstones, a.tombstones_dropped) == (1, 1)


def test_tombstones_count_only_what_the_map_would_have_saved():
    r = TitleReplay(user_id="u1")
    r.record_tombstone("Amazon PPC Specialist", PREFS)
    r.record_tombstone("Senior Golang Engineer", PREFS)
    assert (r.tombstones, r.tombstones_dropped) == (2, 1)


@pytest.mark.parametrize(
    "doc",
    [{}, {"preferences": None}, {"preferences": {}}, {"preferences": {"bogus": 1}}],
)
def test_load_preferences_tolerates_an_unusable_user_doc(doc: dict):
    assert load_preferences(doc) is None


def test_load_preferences_reads_the_real_shape():
    prefs = load_preferences({"preferences": PREFS.model_dump(mode="json")})
    assert prefs is not None
    assert prefs.target_role_families == ["engineering"]


# ── replay_user, against a fake Firestore ───────────────────────────────────


class _Snap:
    def __init__(self, doc: dict, exists: bool = True):
        self._doc = doc
        self.exists = exists

    def to_dict(self):
        return dict(self._doc)


class _Coll:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    async def stream(self):
        for doc in self._docs:
            yield _Snap(doc)


class _Doc:
    def __init__(self, user: dict | None, colls: dict[str, list[dict]]):
        self._user = user
        self._colls = colls

    async def get(self):
        return _Snap(self._user or {}, exists=self._user is not None)

    def collection(self, name: str) -> _Coll:
        return _Coll(self._colls.get(name, []))


class _DB:
    def __init__(self, user: dict | None, colls: dict[str, list[dict]]):
        self._doc = _Doc(user, colls)

    def collection(self, name: str):
        assert name == "users"
        return self

    def document(self, _user_id: str) -> _Doc:
        return self._doc


def _user_doc() -> dict:
    return {"preferences": PREFS.model_dump(mode="json")}


@pytest.mark.asyncio
async def test_replay_user_streams_jobs_and_finds_the_false_drop():
    db = _DB(
        _user_doc(),
        {
            "jobs": [
                {"title": "Playable Ads Editor", "match": {"overall_score": 74}},
                {"title": "Technical Project Manager"},
                {"title": "Senior Software Engineer", "match": {"overall_score": 91}},
                {"company": "acme"},  # no title at all — must not be counted
            ],
            "discarded_jobs": [{"title": "Amazon PPC Specialist", "score": 0}],
        },
    )
    r = await replay_user(db, "u1", with_discarded=False)
    assert r is not None
    assert r.n == 3
    assert r.scored == 2
    assert [f[2] for f in r.false_drops] == ["Playable Ads Editor"]
    assert dict(r.by_family) == {"marketing": 1, "product": 1}
    assert r.tombstones == 0  # not asked for


@pytest.mark.asyncio
async def test_replay_user_reads_tombstones_only_when_asked():
    db = _DB(
        _user_doc(),
        {
            "jobs": [{"title": "Senior Software Engineer"}],
            "discarded_jobs": [
                {"title": "Amazon PPC Specialist"},
                {"title": "Staff Backend Engineer"},
            ],
        },
    )
    r = await replay_user(db, "u1", with_discarded=True)
    assert r is not None
    assert (r.tombstones, r.tombstones_dropped) == (2, 1)


@pytest.mark.asyncio
async def test_replay_user_skips_a_missing_user():
    assert await replay_user(_DB(None, {}), "nobody", with_discarded=False) is None


@pytest.mark.asyncio
async def test_replay_user_reports_a_user_that_cannot_be_filtered():
    db = _DB({}, {"jobs": [{"title": "Playable Ads Editor"}]})
    r = await replay_user(db, "u1", with_discarded=False)
    assert r is not None
    assert r.no_preferences is True
    assert r.by_family == {}


@pytest.mark.asyncio
async def test_replay_user_never_writes(monkeypatch: pytest.MonkeyPatch):
    """A smoke check, not a proof.

    It only catches a write through the document/collection handles this fake
    models; a ``db.batch()`` or a client the fake doesn't model would slip
    past. The read-only property is established by reading the module — this
    just makes the obvious regression loud.
    """

    def _boom(*_a, **_k):
        raise AssertionError("the replay must never write")

    for name in ("set", "update", "delete", "create"):
        monkeypatch.setattr(_Doc, name, _boom, raising=False)
        monkeypatch.setattr(_Coll, name, _boom, raising=False)
    db = _DB(_user_doc(), {"jobs": [{"title": "Playable Ads Editor"}]})
    assert await replay_user(db, "u1", with_discarded=True) is not None


@pytest.mark.asyncio
async def test_pruned_tombstones_are_not_counted_as_pro_rejections():
    """A prune spent no parse and no Pro call, so a drop there saves nothing."""
    db = _DB(
        _user_doc(),
        {
            "jobs": [],
            "discarded_jobs": [
                {"title": "Amazon PPC Specialist", "score": 0},
                {"title": "Amazon PPC Specialist", "pruned": {"by": "prerank"}},
            ],
        },
    )
    r = await replay_user(db, "u1", with_discarded=True)
    assert r is not None
    assert (r.tombstones, r.tombstones_dropped) == (1, 1)
