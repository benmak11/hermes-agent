# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Who may sign in — the machinery, exercised with enforcement switched on.

``tools.allowlist`` ships inert (``ALLOWLIST_ENFORCED`` unset) and every real
caller checks the flag first; the no-op paths at those call sites are pinned
in their own test files. What is pinned here is the module as if the flag were
on, which is the only way to know the machinery is correct before it is
flipped:

1. **The predicate fails closed.** No email, no doc, a malformed doc, a read
   error — every one is "not allowed", never "allowed by default".
2. **The seat cap is enforced by a transaction, not a check-then-write.** Two
   concurrent ``add()``s for the last seat must land exactly one grant.
3. **Revoking frees a seat**, and re-adding a revoked email re-consults the
   cap rather than counting as still held.

No real Firestore: the in-memory fakes are modelled on
``test_scoring_budget.py``'s ``_FakeTransaction`` so the real
``@async_transactional`` decorator and its retry-on-``Aborted`` drive the code
under test.
"""

from __future__ import annotations

import asyncio

import pytest
from google.api_core.exceptions import Aborted

from tools import allowlist

# --------------------------------------------------------------------------
# enforced()
# --------------------------------------------------------------------------


def test_enforced_is_off_by_default(monkeypatch):
    monkeypatch.delenv("ALLOWLIST_ENFORCED", raising=False)
    assert allowlist.enforced() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "on", "ON"])
def test_enforced_recognizes_every_truthy_spelling(monkeypatch, value):
    monkeypatch.setenv("ALLOWLIST_ENFORCED", value)
    assert allowlist.enforced() is True


@pytest.mark.parametrize("value", ["0", "false", "", "  ", "yes"])
def test_enforced_is_off_for_anything_else(monkeypatch, value):
    monkeypatch.setenv("ALLOWLIST_ENFORCED", value)
    assert allowlist.enforced() is False


# --------------------------------------------------------------------------
# Fakes: enough async Firestore for is_allowed / add / revoke / list_entries
# --------------------------------------------------------------------------
class _FakeSnap:
    def __init__(self, doc_id: str, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return None if self._data is None else dict(self._data)


class _FakeDocRef:
    def __init__(self, store: dict, doc_id: str):
        self.store = store
        self.id = doc_id

    async def get(self, transaction=None):
        return _FakeSnap(self.id, self.store.get(self.id))

    async def set(self, data, merge=False):
        if merge:
            self.store.setdefault(self.id, {}).update(data)
        else:
            self.store[self.id] = dict(data)


async def _fake_stream(store: dict):
    for doc_id, data in list(store.items()):
        yield _FakeSnap(doc_id, data)


class _FakeCollRef:
    def __init__(self, store: dict):
        self.store = store

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self.store, doc_id)

    def stream(self, transaction=None):
        return _fake_stream(self.store)


class _FakeTransaction:
    """Enough of AsyncTransaction for the real ``@async_transactional``
    decorator to drive — see ``test_scoring_budget.py``'s twin for the same
    trick played against ``budget.reserve``."""

    _read_only = False
    _max_attempts = 5

    def __init__(self, abort_once: bool = False):
        self._id = None
        self._buffered: list[tuple[_FakeDocRef, dict]] = []
        self._abort_once = abort_once
        self.commits = 0

    def _clean_up(self):
        self._buffered = []

    async def _begin(self, retry_id=None):
        self._id = b"txn"

    async def _rollback(self):
        self._buffered = []

    async def _commit(self):
        if self._abort_once:
            self._abort_once = False
            raise Aborted("contended")
        for ref, data in self._buffered:
            ref.store[ref.id] = data
        self.commits += 1
        return []

    def set(self, reference: _FakeDocRef, document_data, merge=False):
        self._buffered.append((reference, dict(document_data)))


class _FakeDB:
    """A flat ``allowlist`` collection: ``email_key -> doc``."""

    def __init__(self, docs: dict[str, dict] | None = None, abort_once: bool = False):
        self.store: dict[str, dict] = dict(docs or {})
        self._abort_once = abort_once
        self.transactions: list[_FakeTransaction] = []

    def collection(self, name: str) -> _FakeCollRef:
        assert name == allowlist.COLLECTION
        return _FakeCollRef(self.store)

    def transaction(self) -> _FakeTransaction:
        txn = _FakeTransaction(abort_once=self._abort_once)
        self._abort_once = False
        self.transactions.append(txn)
        return txn


class _BrokenDB:
    """Every call explodes — the read-error arm of "fails closed"."""

    def collection(self, name: str):
        raise RuntimeError("firestore down")


# --------------------------------------------------------------------------
# is_allowed — fully fails closed
# --------------------------------------------------------------------------


def test_is_allowed_true_for_an_active_entry():
    db = _FakeDB({"user@example.com": {"email": "user@example.com", "revoked": False}})
    assert asyncio.run(allowlist.is_allowed(db, "User@Example.com")) is True


def test_is_allowed_normalizes_case_and_whitespace_on_both_sides():
    db = _FakeDB({"user@example.com": {"revoked": False}})
    assert asyncio.run(allowlist.is_allowed(db, "  USER@EXAMPLE.com  ")) is True


@pytest.mark.parametrize("email", [None, "", "   "])
def test_is_allowed_false_for_no_email(email):
    db = _FakeDB({"user@example.com": {"revoked": False}})
    assert asyncio.run(allowlist.is_allowed(db, email)) is False


def test_is_allowed_false_for_a_missing_document():
    db = _FakeDB()
    assert asyncio.run(allowlist.is_allowed(db, "nobody@example.com")) is False


def test_is_allowed_false_for_a_revoked_entry():
    db = _FakeDB({"user@example.com": {"revoked": True}})
    assert asyncio.run(allowlist.is_allowed(db, "user@example.com")) is False


def test_is_allowed_false_for_a_read_error():
    """Fail closed, the opposite bias from the discovery guards — see the
    module docstring for why that asymmetry is deliberate, not inconsistent."""
    assert asyncio.run(allowlist.is_allowed(_BrokenDB(), "user@example.com")) is False


def test_is_allowed_false_for_a_non_mapping_document():
    """A hand-edited or corrupted doc must not grant access by accident."""

    class _WeirdSnap:
        exists = True

        def to_dict(self):
            return "not-a-dict"

    class _WeirdDocRef:
        async def get(self, transaction=None):
            return _WeirdSnap()

    class _WeirdDB:
        def collection(self, name):
            return type("C", (), {"document": lambda self, i: _WeirdDocRef()})()

    assert asyncio.run(allowlist.is_allowed(_WeirdDB(), "user@example.com")) is False


def test_is_allowed_false_for_a_malformed_revoked_field():
    """``revoked`` holding garbage still has to fail closed — the safe
    direction even when the garbage happens to say something falsy-looking."""
    db = _FakeDB({"user@example.com": {"revoked": "not-a-bool"}})
    assert asyncio.run(allowlist.is_allowed(db, "user@example.com")) is False


# --------------------------------------------------------------------------
# add — the transactional seat cap
# --------------------------------------------------------------------------


def test_add_creates_a_seat_under_the_cap():
    db = _FakeDB()
    ok = asyncio.run(allowlist.add(db, "New@Example.com", added_by="op", max_users=5))
    assert ok is True
    doc = db.store["new@example.com"]
    assert doc["email"] == "new@example.com"
    assert doc["added_by"] == "op"
    assert doc["revoked"] is False


def test_add_refuses_once_the_cap_is_full():
    db = _FakeDB({f"user{i}@example.com": {"revoked": False} for i in range(3)})
    ok = asyncio.run(allowlist.add(db, "new@example.com", added_by="op", max_users=3))
    assert ok is False
    assert "new@example.com" not in db.store


def test_add_does_not_count_revoked_seats_against_the_cap():
    db = _FakeDB(
        {
            "user1@example.com": {"revoked": False},
            "user2@example.com": {"revoked": True},
        }
    )
    ok = asyncio.run(allowlist.add(db, "new@example.com", added_by="op", max_users=2))
    assert ok is True


def test_re_adding_an_already_active_email_is_a_no_op_success():
    db = _FakeDB({"user@example.com": {"revoked": False, "note": "original"}})
    ok = asyncio.run(
        allowlist.add(db, "User@Example.com", added_by="op", max_users=1, note="new")
    )
    assert ok is True
    assert db.store["user@example.com"]["note"] == "original"  # untouched


def test_re_adding_a_revoked_email_re_checks_the_cap():
    db = _FakeDB(
        {
            "user1@example.com": {"revoked": False},
            "user2@example.com": {"revoked": True},
        }
    )
    ok = asyncio.run(allowlist.add(db, "user2@example.com", added_by="op", max_users=1))
    assert ok is False  # one active seat already, cap is one
    assert db.store["user2@example.com"]["revoked"] is True  # untouched


def test_add_failures_propagate():
    """Fail closed: a caller that can't check the seat count can't safely
    grant one either — guessing permissive is what a seat cap must not do."""
    with pytest.raises(RuntimeError):
        asyncio.run(
            allowlist.add(_BrokenDB(), "user@example.com", added_by="op", max_users=5)
        )


# --- the mutation-check: the seat count has to be read *inside* the txn ----


def test_two_sequential_adds_for_the_last_seat_grant_exactly_one():
    """Two calls back to back, each re-deriving the count from what the
    previous one actually persisted — not from a value carried in memory
    across calls. Real, but not by itself proof of isolation: a strictly
    sequential pair can't tell "read inside the transaction" apart from "read
    right before opening one", because nothing else can have written in
    between either way. See the test below for the property this one can't
    reach.
    """
    db = _FakeDB({"user1@example.com": {"revoked": False}})
    max_users = 2
    first = asyncio.run(
        allowlist.add(db, "a@example.com", added_by="op", max_users=max_users)
    )
    second = asyncio.run(
        allowlist.add(db, "b@example.com", added_by="op", max_users=max_users)
    )

    assert (first, second) == (True, False)
    active = sum(1 for d in db.store.values() if not d.get("revoked"))
    assert active == max_users, "the cap was exceeded — the isolation broke"


def test_the_seat_count_and_the_grant_travel_through_the_same_transaction(
    monkeypatch,
):
    """The count read must share a transaction object with the write it gates.

    That is what gives Firestore's optimistic-concurrency check something to
    attach to. A fake cannot reproduce the server's conflict detection, but it
    can pin this structural fact, and it is what catches ``count() < MAX``
    checked before ``db.transaction()`` opens — the sequential-call tests
    around it cannot, because a stale-but-separate count still adds up when
    nothing runs in between.
    """
    seen: dict = {}
    real_active_seats = allowlist._active_seats

    async def spying_active_seats(db, transaction):
        seen["count_txn"] = transaction
        return await real_active_seats(db, transaction)

    real_set = _FakeTransaction.set

    def spying_set(self, reference, document_data, merge=False):
        seen["write_txn"] = self
        return real_set(self, reference, document_data, merge=merge)

    monkeypatch.setattr(allowlist, "_active_seats", spying_active_seats)
    monkeypatch.setattr(_FakeTransaction, "set", spying_set)

    db = _FakeDB()
    asyncio.run(allowlist.add(db, "new@example.com", added_by="op", max_users=5))

    assert "count_txn" in seen and "write_txn" in seen
    assert seen["count_txn"] is seen["write_txn"]


def test_a_contended_add_retries_without_double_granting():
    """The transaction's own retry-on-``Aborted`` re-reads the seat count on
    the second attempt rather than trusting the first attempt's stale view."""
    db = _FakeDB({"user1@example.com": {"revoked": False}}, abort_once=True)
    ok = asyncio.run(allowlist.add(db, "new@example.com", added_by="op", max_users=2))
    assert ok is True
    active = sum(1 for d in db.store.values() if not d.get("revoked"))
    assert active == 2  # not 3 — the aborted attempt's write never landed


# --------------------------------------------------------------------------
# revoke
# --------------------------------------------------------------------------


def test_revoke_marks_an_entry_revoked():
    db = _FakeDB({"user@example.com": {"revoked": False}})
    ok = asyncio.run(allowlist.revoke(db, "User@Example.com", revoked_by="op"))
    assert ok is True
    assert db.store["user@example.com"]["revoked"] is True
    assert db.store["user@example.com"]["revoked_by"] == "op"


def test_revoke_of_a_missing_email_is_a_no_op():
    db = _FakeDB()
    ok = asyncio.run(allowlist.revoke(db, "nobody@example.com", revoked_by="op"))
    assert ok is False
    assert db.store == {}


def test_a_revoked_seat_frees_the_cap_for_the_next_add():
    db = _FakeDB({"user1@example.com": {"revoked": False}})
    asyncio.run(allowlist.revoke(db, "user1@example.com", revoked_by="op"))

    ok = asyncio.run(allowlist.add(db, "user2@example.com", added_by="op", max_users=1))

    assert ok is True


# --------------------------------------------------------------------------
# list_entries
# --------------------------------------------------------------------------


def test_list_entries_carries_the_key_as_email():
    db = _FakeDB({"user@example.com": {"added_by": "op", "revoked": False}})
    entries = asyncio.run(allowlist.list_entries(db))
    assert entries == [
        {"email": "user@example.com", "added_by": "op", "revoked": False}
    ]


def test_list_entries_of_an_empty_allowlist():
    assert asyncio.run(allowlist.list_entries(_FakeDB())) == []


# --------------------------------------------------------------------------
# seat_cap()
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "cap"), [("25", 25), (" 7 ", 7), ("1", 1)])
def test_seat_cap_reads_a_positive_max_users(monkeypatch, raw, cap):
    monkeypatch.setenv("MAX_USERS", raw)
    assert allowlist.seat_cap() == cap


@pytest.mark.parametrize("raw", ["", "  ", "ten", "2.5", "0", "-1"])
def test_seat_cap_is_none_for_anything_unusable(monkeypatch, raw):
    monkeypatch.setenv("MAX_USERS", raw)
    assert allowlist.seat_cap() is None


def test_seat_cap_is_none_when_unset(monkeypatch):
    monkeypatch.delenv("MAX_USERS", raising=False)
    assert allowlist.seat_cap() is None
