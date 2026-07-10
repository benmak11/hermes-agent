# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Cross-user JD parse cache: a posting parses once, ever.

Job boards serve the same posting to every user (and every re-discovery after
a purge), but parses were stored only on per-user job docs — so the same
jd_raw could be paid for repeatedly. This module caches ``ParsedJD`` results
in a top-level ``jd_cache`` collection keyed by the SHA-256 of the raw JD
text: both scoring paths consult it before spending a Flash call and write
back after any fresh parse.

The cache holds only posting content (no user data), so sharing across users
is safe. Staleness self-heals: if ``ParsedJD`` grows a field and an old cache
doc no longer validates, the lookup misses, the JD re-parses, and the store
overwrites the doc.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from google.cloud import firestore
from pydantic import ValidationError

from models.job import ParsedJD
from obs.logging import get_logger

log = get_logger("tools.matching.jd_cache")

COLLECTION = "jd_cache"

# Firestore caps a WriteBatch at 500 operations.
_WRITE_CHUNK = 500


def jd_hash(jd_raw: str) -> str:
    """Content key for a raw JD. Exact-match by design — no normalization."""
    return hashlib.sha256(jd_raw.encode()).hexdigest()


async def lookup_many(
    db: firestore.AsyncClient, texts: list[str]
) -> dict[str, ParsedJD]:
    """Cached parses for the given JD texts, keyed by the original text.

    Misses (and cache docs that no longer validate against the current
    ``ParsedJD`` schema) are simply absent from the result.
    """
    by_hash = {jd_hash(t): t for t in texts if t.strip()}
    if not by_hash:
        return {}
    refs = [db.collection(COLLECTION).document(h) for h in by_hash]
    found: dict[str, ParsedJD] = {}
    async for snap in db.get_all(refs):
        if not snap.exists:
            continue
        try:
            parsed = ParsedJD.model_validate((snap.to_dict() or {}).get("jd_parsed"))
        except ValidationError:
            continue  # schema drift → treat as a miss; store() will overwrite
        found[by_hash[snap.id]] = parsed
    return found


async def lookup(db: firestore.AsyncClient, jd_raw: str) -> ParsedJD | None:
    """Single-JD convenience wrapper around :func:`lookup_many`."""
    return (await lookup_many(db, [jd_raw])).get(jd_raw)


async def store_many(
    db: firestore.AsyncClient, parses: dict[str, ParsedJD], *, model: str
) -> None:
    """Write fresh parse results back to the cache (chunked batch writes).

    Best-effort like ``persist_jd_parsed``: the run already has its parses in
    hand, so a failed cache write only costs a future run a re-parse.
    """
    entries = [(jd_hash(text), parsed) for text, parsed in parses.items()]
    now = datetime.now(UTC).isoformat()
    try:
        for start in range(0, len(entries), _WRITE_CHUNK):
            batch = db.batch()
            for h, parsed in entries[start : start + _WRITE_CHUNK]:
                batch.set(
                    db.collection(COLLECTION).document(h),
                    {
                        "jd_parsed": parsed.model_dump(mode="json"),
                        "model": model,
                        "created_at": now,
                    },
                )
            await batch.commit()
    except Exception:
        log.warning("jd_cache.store_failed", count=len(entries))


async def store(
    db: firestore.AsyncClient, jd_raw: str, parsed: ParsedJD, *, model: str
) -> None:
    """Single-JD convenience wrapper around :func:`store_many`."""
    await store_many(db, {jd_raw: parsed}, model=model)
