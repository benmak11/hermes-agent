# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""A user-document Firestore stand-in that honours transactions.

Shared because five test modules now reach code that charges a budget inside a
transaction, and a fake per module is how one of them ends up with a
transaction that quietly commits nothing.

Faked at the protocol the real ``@async_transactional`` / ``@transactional``
decorators drive — begin, buffer, commit, rollback, retry on ``Aborted`` — so
the decorator's own behaviour is exercised rather than stubbed out. Writes are
applied **through the reference**, the way Firestore does, which is what lets
the same transaction drive any document fake with a ``set``.
"""

from __future__ import annotations

from google.api_core.exceptions import Aborted

from tools.discovery import budget


class _FakeSnap:
    """``exists`` is modelled separately from emptiness, because the code
    under test distinguishes them: a user document that is merely *empty* is a
    live account with no counters yet, while one that is *absent* has been
    wiped and must not be written back into existence."""

    def __init__(self, doc, exists=True):
        self._doc = doc
        self.exists = exists

    def to_dict(self):
        return dict(self._doc)


class _FakeAsyncDoc:
    def __init__(self, store, exists=True):
        self._store = store
        self._exists = exists

    async def get(self, transaction=None):
        return _FakeSnap(self._store, self._exists)

    def set(self, data, merge=False):
        """Sync on purpose: a transaction's writes land at commit, and the
        fake transaction below is what calls this."""
        if not merge:
            self._store.clear()
        self._store.update(data)


class _FakeSyncDoc(_FakeAsyncDoc):
    def get(self, transaction=None):  # type: ignore[override]
        return _FakeSnap(self._store, self._exists)


class FakeTransaction:
    """Writes buffer until commit, so a losing attempt re-reads the winner.

    Applied **through the reference**, the way Firestore does, rather than
    straight into a store this object happens to hold: that is what lets the
    same fake drive any document fake with a ``set``, and it is the difference
    between testing a transaction and testing a dictionary.
    """

    _read_only = False
    _max_attempts = 5

    def __init__(self, abort_once=False):
        self._id = None
        self._buffered: list[tuple] = []
        self._abort_once = abort_once
        self.commits = 0

    def _clean_up(self):
        self._buffered = []

    def set(self, reference, document_data, merge=False):
        self._buffered.append((reference, dict(document_data), merge))

    def _apply(self):
        if self._abort_once:
            self._abort_once = False
            raise Aborted("contended")
        for reference, data, merge in self._buffered:
            reference.set(data, merge=merge)
        self._buffered = []
        self.commits += 1
        return []


class FakeAsyncTransaction(FakeTransaction):
    async def _begin(self, retry_id=None):
        self._id = b"txn"

    async def _rollback(self):
        self._buffered = []

    async def _commit(self):
        return self._apply()


class FakeSyncTransaction(FakeTransaction):
    def _begin(self, retry_id=None):
        self._id = b"txn"

    def _rollback(self):
        self._buffered = []

    def _commit(self):
        return self._apply()


class _FakeCollection:
    def __init__(self, doc):
        self._doc = doc

    def document(self, doc_id):
        return self._doc


class _FakeDB:
    """Just enough Firestore for ``users/{uid}`` under a transaction."""

    sync = False

    def __init__(self, state=None, abort_once=False, exists=True):
        self.store: dict = {budget.FIELD: state} if state else {}
        self._abort_once = abort_once
        self.exists = exists
        self.transactions: list[FakeTransaction] = []

    def collection(self, name):
        assert name == "users", name
        cls = _FakeSyncDoc if self.sync else _FakeAsyncDoc
        return _FakeCollection(cls(self.store, self.exists))

    def transaction(self):
        cls = FakeSyncTransaction if self.sync else FakeAsyncTransaction
        txn = cls(abort_once=self._abort_once)
        self._abort_once = False
        self.transactions.append(txn)
        return txn

    @property
    def budget_state(self) -> dict:
        return self.store[budget.FIELD]


class _FakeSyncDB(_FakeDB):
    sync = True
