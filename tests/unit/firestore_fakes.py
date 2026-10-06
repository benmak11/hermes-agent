# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Shared Firestore stand-ins: a transaction-driving user-document fake, and a
read-only query fake.

The transaction fake exists because several test modules reach code that
charges a budget inside a transaction, and a fake per module is how one of
them ends up with a transaction that quietly commits nothing.

The query fake (:class:`FakeQueryDB`) honours every constraint a query
carries — ``where`` filters, ``order_by`` sorts, ``limit`` truncates after
ordering, ``select`` projects — because a fake that returns ``self`` from those
hides the bugs the tests exist to catch. It refuses a query that would need a composite index and
has no write methods.

The transaction fake is driven at the protocol the real
``@async_transactional`` / ``@transactional`` decorators drive — begin, buffer, commit, rollback, retry on ``Aborted`` — so
the decorator's own behaviour is exercised rather than stubbed out. Writes are
applied **through the reference**, the way Firestore does, which is what lets
the same transaction drive any document fake with a ``set``.
"""

from __future__ import annotations

from google.api_core.exceptions import Aborted
from google.cloud import firestore

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


# ------------------------------------------------------- read-only query fake


class _QuerySnap:
    def __init__(self, doc_id, doc):
        self.id = doc_id
        self._doc = doc
        self.exists = doc is not None

    def to_dict(self):
        return dict(self._doc) if self._doc is not None else None


class _Query:
    """Constraints are collected and applied at ``stream`` in Firestore's
    order — filter, then sort, then limit — whatever order they were chained."""

    def __init__(self, db, path, docs, filters=(), orders=(), limit=None, fields=None):
        self._db = db
        self._path = path
        self._docs = docs
        self._filters = tuple(filters)
        self._orders = tuple(orders)
        self._limit = limit
        self._fields = fields

    def _with(self, **kw):
        args = {
            "filters": self._filters,
            "orders": self._orders,
            "limit": self._limit,
            "fields": self._fields,
        }
        args.update(kw)
        return _Query(self._db, self._path, self._docs, **args)

    def where(self, *, filter):
        assert filter.op_string == "==", filter.op_string
        return self._with(filters=(*self._filters, (filter.field_path, filter.value)))

    def order_by(self, field_path, direction=firestore.Query.ASCENDING):
        return self._with(orders=(*self._orders, (field_path, direction)))

    def limit(self, count):
        return self._with(limit=count)

    def select(self, field_paths):
        return self._with(fields=tuple(field_paths))

    async def stream(self):
        filtered = {f for f, _ in self._filters}
        ordered = {f for f, _ in self._orders}
        if filtered and ordered - filtered:
            raise AssertionError(
                f"{self._path}: where on {filtered} + order_by on {ordered} "
                "needs a composite index that does not exist"
            )
        self._db.queries.append((self._path, self._filters, self._orders, self._limit))
        rows = list(self._docs.items())
        for field_path, value in self._filters:
            rows = [(i, d) for i, d in rows if d.get(field_path) == value]
        for field_path, direction in reversed(self._orders):
            # Firestore drops documents that lack an ordered field.
            rows = [(i, d) for i, d in rows if field_path in d]
            rows.sort(
                key=lambda r, f=field_path: r[1][f],
                reverse=direction == firestore.Query.DESCENDING,
            )
        if self._limit is not None:
            rows = rows[: self._limit]
        if self._fields is not None:
            # Like Firestore, a projection still returns every matching
            # document, carrying only the selected fields it actually has.
            self._db.selects.append((self._path, self._fields))
            rows = [(i, {f: d[f] for f in self._fields if f in d}) for i, d in rows]
        for doc_id, doc in rows:
            yield _QuerySnap(doc_id, doc)


class _QueryColl(_Query):
    def document(self, doc_id):
        return _QueryDoc(self._db, f"{self._path}/{doc_id}", self._docs.get(doc_id))


class _QueryDoc:
    def __init__(self, db, path, doc):
        self._db = db
        self._path = path
        self._doc = doc

    async def get(self):
        self._db.gets.append(self._path)
        return _QuerySnap(self._path.rsplit("/", 1)[-1], self._doc)

    def collection(self, name):
        path = f"{self._path}/{name}"
        return _QueryColl(self._db, path, self._db.data.setdefault(path, {}))


class FakeQueryDB:
    """``data`` maps a collection path to ``{doc_id: dict}``. Reads only;
    ``queries``, ``selects`` and ``gets`` record what was asked, for
    assertions."""

    def __init__(self, data=None):
        self.data = data or {}
        self.queries: list = []
        self.selects: list = []
        self.gets: list[str] = []

    def collection(self, name):
        return _QueryColl(self, name, self.data.setdefault(name, {}))
