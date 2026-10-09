# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Measure ``tools.matching.prerank`` against the scores Pro already assigned.

READ-ONLY and free: streams Firestore with field projections, writes nothing,
calls no model. ``--with-cache`` adds batched reads of the top-level
``jd_cache`` (still read-only) and pulls ``jd_raw`` for every pending doc.

Corpus, per user:

- **J** — every ``users/{uid}/jobs`` doc carrying a ``match``, any decision.
- **T** — every ``users/{uid}/discarded_jobs`` tombstone a model scored.
  Tombstones ``cli.prune_backlog`` wrote carry no score and are never labels.

``y = 1`` iff the outcome score (``match.overall_score`` on J, ``score`` on T)
is at least the queue threshold.

**The gate masks ``location`` and ``parsed``.** Those inputs, and
``discovered_at``, exist on J but mostly not on T, and J is mostly survivors
of Pro's own cut, so any feature built on them predicts *which collection a
row came from* and reads as signal. The gating AUC uses neither and never
looks at ``discovered_at``. The per-feature table reads each term from the
masked prerank, except the J-only ``parsed``, which is reported unmasked and
marked. ``location`` carries no weight; its signal is reported as a diagnostic.

AUC is per user. With several ``--user-id``\\s the pooled figure sums each
user's Mann-Whitney U and pair count, so only within-user pairs are compared.

Section 7 tabulates prune cutoffs: per cutoff, the labelled rows at or below
it by the masked prerank, and the backlog jobs at or below it by the unmasked
prerank ``cli.prune_backlog`` uses.

Usage:
    python -m cli.prerank_eval --user-id <uid>
    python -m cli.prerank_eval --user-id <uid> --cutoff -25 --cutoff -15
    TITLE_FILTER_WIDE=1 python -m cli.prerank_eval --user-id <uid>
    python -m cli.prerank_eval --user-id <uid> --half even --with-cache
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import os
from collections import Counter
from dataclasses import dataclass, field

from dotenv import load_dotenv
from google.cloud.firestore_v1.base_query import FieldFilter

from cli.eval_scoring import Auc, Undefined, auc, average_ranks, firestore_client
from obs.logging import bind_run_context
from tools.matching import jd_cache
from tools.matching.pipeline import geo_enforce_enabled
from tools.matching.prerank import (
    FEATURES,
    PRERANK_VERSION,
    W_PARSED_REJECT,
    Prerank,
    preferences_from,
    prerank,
    residence_country,
)
from tools.matching.score import QUEUE_DEFAULT_MIN_SCORE, is_pruned

# Pins GOOGLE_CLOUD_PROJECT; ``firestore_client`` asserts it is set.
load_dotenv()

JOBS = "jobs"
TOMBSTONES = "discarded_jobs"

#: Terms built on fields only J carries; the gate must never see them.
GATE_MASK = frozenset({"location", "parsed"})

#: Terms reported as a signal diagnostic rather than a feature row.
SIGNAL_ONLY = frozenset({"location"})

#: How many foreign-signal positives section 3 lists.
FOREIGN_LIST_LIMIT = 10

#: Projections. Neither reads ``jd_raw``, nor a tombstone's ``restore``
#: payload, which carries the J-only ``location`` and ``discovered_at``.
JOB_FIELDS = [
    "title",
    "company",
    "location",
    "jd_parsed",
    "discovered_at",
    "match",
    "user_decision",
]
TOMBSTONE_FIELDS = ["title", "company", "score", "jd_parsed", "pruned"]

PASS_AUC = 0.70
PASS_LO = 0.60
MIN_PER_CLASS = 30
TOP_FRACTION = 0.10
CACHE_CHUNK = 300

#: Section 7's default prune cutoffs; ``--cutoff`` replaces them.
PRUNE_CUTOFFS = (-40.0, -30.0, -20.0, -10.0, -5.0, 0.0, 5.0)


# ------------------------------------------------------------------- corpus


@dataclass(frozen=True)
class Row:
    """One labelled job: its outcome score and both preranks."""

    job_id: str
    source: str
    doc: dict
    outcome: float
    gate: Prerank
    full: Prerank

    @property
    def positive(self) -> bool:
        return self.outcome >= QUEUE_DEFAULT_MIN_SCORE


@dataclass
class Backlog:
    """Pending, unscored jobs, ranked unmasked: this is what prerank would
    order."""

    job_ids: list[str] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    foreign_signals: int = 0
    with_parse: int = 0
    unparsed: int = 0
    cache_hits: int | None = None
    cache_rejects: int | None = None


@dataclass
class Corpus:
    user_id: str
    has_preferences: bool
    residence: str | None
    rows: list[Row] = field(default_factory=list)
    backlog: Backlog = field(default_factory=Backlog)


def _hash(job_id: str) -> int:
    return int.from_bytes(hashlib.sha256(job_id.encode()).digest(), "big")


def in_half(job_id: str, half: str | None) -> bool:
    """``--half`` selection by the parity of ``sha256(id)``."""
    if half is None:
        return True
    return (_hash(job_id) % 2 == 0) == (half == "even")


def content_blind(job_id: str) -> float:
    """Baseline B1: a score from the id alone, independent of the hash bit
    ``--half`` uses."""
    return float(_hash(job_id) >> 192)


def is_pending_unscored(doc: dict) -> bool:
    """``score.load_profile_and_pending``'s predicate: a ``pending`` decision
    and no ``match`` key."""
    return doc.get("user_decision") == "pending" and "match" not in doc


def outcome_score(doc: dict, source: str) -> float | None:
    """``match.overall_score`` on a job doc, ``score`` on a tombstone."""
    if source == JOBS:
        match = doc.get("match")
        value = match.get("overall_score") if isinstance(match, dict) else None
    else:
        value = doc.get("score")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


async def _cache_backlog(db, jobs_ref, backlog: Backlog, rank) -> None:
    """jd_cache hits among pending unparsed jobs, and how many of those the
    prefilter would reject. Reads ``jd_raw``, so only behind ``--with-cache``."""
    query = jobs_ref.where(filter=FieldFilter("user_decision", "==", "pending"))
    docs: dict[str, dict] = {}
    async for snap in query.select(["jd_raw", *JOB_FIELDS]).stream():
        doc = snap.to_dict() or {}
        if is_pending_unscored(doc) and doc.get("jd_parsed") is None:
            if isinstance(doc.get("jd_raw"), str):
                docs[snap.id] = doc
    texts = list({doc["jd_raw"] for doc in docs.values()})
    found = {}
    for start in range(0, len(texts), CACHE_CHUNK):
        found.update(await jd_cache.lookup_many(db, texts[start : start + CACHE_CHUNK]))
    hits = rejects = 0
    for job_id, doc in docs.items():
        parsed = found.get(doc["jd_raw"])
        if parsed is None:
            continue
        hits += 1
        with_parse = {**doc, "jd_parsed": parsed.model_dump(mode="json")}
        if rank(job_id, with_parse).features.get("parsed") == W_PARSED_REJECT:
            rejects += 1
    backlog.cache_hits, backlog.cache_rejects = hits, rejects


async def load_user(
    db,
    user_id: str,
    *,
    half: str | None = None,
    enforce_geo: bool = False,
    with_cache: bool = False,
) -> Corpus | None:
    """Read one user's corpus and backlog. ``None`` when the user is absent."""
    user_ref = db.collection("users").document(user_id)
    snap = await user_ref.get()
    if not snap.exists:
        return None
    user_doc = snap.to_dict() or {}
    prefs = preferences_from(user_doc)
    residence = residence_country(user_doc)
    corpus = Corpus(user_id, prefs is not None, residence)

    def rank(job_id: str, doc: dict, mask=frozenset()) -> Prerank:
        return prerank(
            {**doc, "id": job_id}, prefs, residence, mask=mask, enforce_geo=enforce_geo
        )

    def add(job_id: str, doc: dict, source: str) -> None:
        score = outcome_score(doc, source)
        if score is None or not in_half(job_id, half):
            return
        corpus.rows.append(
            Row(
                job_id,
                source,
                doc,
                score,
                gate=rank(job_id, doc, GATE_MASK),
                full=rank(job_id, doc),
            )
        )

    jobs_ref = user_ref.collection(JOBS)
    backlog = corpus.backlog
    async for job_snap in jobs_ref.select(JOB_FIELDS).stream():
        doc = job_snap.to_dict() or {}
        if "match" in doc:
            add(job_snap.id, doc, JOBS)
        elif is_pending_unscored(doc):
            result = rank(job_snap.id, doc)
            backlog.job_ids.append(job_snap.id)
            backlog.scores.append(result.score)
            backlog.foreign_signals += result.signals.get("location") == "foreign"
            if doc.get("jd_parsed") is not None:
                backlog.with_parse += 1
            else:
                backlog.unparsed += 1

    async for tomb in user_ref.collection(TOMBSTONES).select(TOMBSTONE_FIELDS).stream():
        doc = tomb.to_dict() or {}
        if not is_pruned(doc):
            add(tomb.id, doc, TOMBSTONES)

    if with_cache:
        await _cache_backlog(db, jobs_ref, backlog, rank)
    return corpus


# ----------------------------------------------------------------- metrics


def split_scores(rows: list[Row], score) -> tuple[list[float], list[float]]:
    """``(positives, negatives)`` of ``score(row)``."""
    fit = [score(r) for r in rows if r.positive]
    unfit = [score(r) for r in rows if not r.positive]
    return fit, unfit


def mann_whitney_u(fit: list[float], unfit: list[float]) -> tuple[float, int]:
    """Tie-corrected U and the pair count, the two numbers a pooled AUC sums."""
    ranks = average_ranks(fit + unfit)
    n = len(fit)
    return sum(ranks[:n]) - n * (n + 1) / 2.0, n * len(unfit)


def pooled_auc(per_user: list[tuple[list[float], list[float]]]) -> float | Undefined:
    """AUC over within-user pairs only: summed U over summed pairs."""
    u_total = pairs_total = 0.0
    for fit, unfit in per_user:
        if fit and unfit:
            u, pairs = mann_whitney_u(fit, unfit)
            u_total += u
            pairs_total += pairs
    if not pairs_total:
        return Undefined("no user has both classes")
    return u_total / pairs_total


def precision_at_top(
    scores: list[float], labels: list[bool], fraction: float = TOP_FRACTION
) -> float | Undefined:
    """Precision of the top ``fraction`` by score. A tie block straddling the
    cut-off counts fractionally: each of its slots inside the cut is worth the
    block's positive share."""
    n = len(scores)
    if not n:
        return Undefined("no rows")
    k = max(1, math.ceil(n * fraction))
    blocks: dict[float, list[int]] = {}
    for score, label in zip(scores, labels, strict=True):
        block = blocks.setdefault(score, [0, 0])
        block[0] += 1
        block[1] += int(label)
    taken = hits = 0.0
    for score in sorted(blocks, reverse=True):
        count, positives = blocks[score]
        take = min(count, k - taken)
        hits += take * positives / count
        taken += take
        if taken >= k:
            break
    return hits / k


def false_buries(rows: list[Row]) -> list[Row]:
    """Positives ranked strictly below the median by the gating prerank. A
    row tied at the median is not buried."""
    if not rows:
        return []
    ranks = average_ranks([r.gate.score for r in rows])
    middle = (len(rows) + 1) / 2.0
    buried = [
        r for r, rank in zip(rows, ranks, strict=True) if r.positive and rank < middle
    ]
    return sorted(buried, key=lambda r: (r.gate.score, r.job_id))


def location_falsifications(rows: list[Row]) -> list[Row]:
    """J positives the ``location`` term penalises: each one is a job Pro
    rated worth showing that the term would have pushed down. Empty while the
    term has no weight; it gates again if the weight goes negative."""
    return [
        r
        for r in rows
        if r.source == JOBS and r.positive and r.full.features.get("location", 0.0) < 0
    ]


def _foreign(row: Row) -> bool:
    return row.full.signals.get("location") == "foreign"


def foreign_positives(rows: list[Row]) -> list[Row]:
    """Positives carrying the ``foreign`` location signal, by id. Diagnostic
    only: it never gates."""
    return sorted(
        (r for r in rows if r.positive and _foreign(r)), key=lambda r: r.job_id
    )


@dataclass(frozen=True)
class SignalStat:
    overall: float
    positives: float


def location_signal_stats(rows: list[Row]) -> SignalStat:
    """Share of rows, and of positives, carrying the ``foreign`` signal."""
    pos = [r for r in rows if r.positive]
    return SignalStat(
        overall=sum(map(_foreign, rows)) / len(rows) if rows else 0.0,
        positives=sum(map(_foreign, pos)) / len(pos) if pos else 0.0,
    )


@dataclass(frozen=True)
class FeatureStat:
    auc: Auc | Undefined
    fires: float
    lift: float | Undefined


def _feature_value(row: Row, name: str) -> float:
    """A term's contribution as the table reports it: from the masked prerank,
    or the full one for a J-only term the gate masks. Reading ``family`` from
    the full prerank would hide it wherever a stored parse replaced it."""
    source = row.full if name in GATE_MASK else row.gate
    return source.features.get(name, 0.0)


def feature_stats(rows: list[Row]) -> dict[str, FeatureStat]:
    """Each weighted term alone: AUC, share of rows where it is non-zero, and
    lift (positive rate where it fires over the base rate)."""
    out: dict[str, FeatureStat] = {}
    base = sum(r.positive for r in rows) / len(rows) if rows else 0.0
    for name in FEATURES:
        if name in SIGNAL_ONLY:
            continue
        fit, unfit = split_scores(rows, lambda r, n=name: _feature_value(r, n))
        firing = [r for r in rows if _feature_value(r, name) != 0]
        if not firing:
            lift: float | Undefined = Undefined("never fires")
        elif not base:
            lift = Undefined("no positives")
        else:
            lift = (sum(r.positive for r in firing) / len(firing)) / base
        out[name] = FeatureStat(
            auc=auc(fit, unfit),
            fires=len(firing) / len(rows) if rows else 0.0,
            lift=lift,
        )
    return out


@dataclass(frozen=True)
class Gate:
    masked: Auc | Undefined
    b1: Auc | Undefined
    b2: Auc | Undefined
    n_pos: int
    n_neg: int
    falsifications: int
    verdict: str
    reasons: tuple[str, ...]


def gate(rows: list[Row]) -> Gate:
    """The masked AUC, both baselines, and the PASS/FAIL/UNDEFINED verdict."""
    fit, unfit = split_scores(rows, lambda r: r.gate.score)
    masked = auc(fit, unfit)
    b1 = auc(*split_scores(rows, lambda r: content_blind(r.job_id)))
    b2 = auc(*split_scores(rows, lambda r: r.gate.features.get("title_exact", 0.0)))
    falsified = len(location_falsifications(rows))
    n_pos, n_neg = len(fit), len(unfit)

    if n_pos < MIN_PER_CLASS or n_neg < MIN_PER_CLASS or isinstance(masked, Undefined):
        reason = (
            f"needs >= {MIN_PER_CLASS} per class; have {n_pos} positive / "
            f"{n_neg} negative"
        )
        return Gate(masked, b1, b2, n_pos, n_neg, falsified, "UNDEFINED", (reason,))

    reasons = []
    if masked.value < PASS_AUC:
        reasons.append(f"AUC {masked.value:.3f} < {PASS_AUC}")
    if masked.lo < PASS_LO:
        reasons.append(f"CI lower bound {masked.lo:.3f} < {PASS_LO}")
    if not isinstance(b2, Undefined) and masked.value < b2.value:
        reasons.append(f"AUC below title_exact alone ({b2.value:.3f})")
    if falsified:
        reasons.append(f"{falsified} location falsification(s)")
    verdict = "FAIL" if reasons else "PASS"
    return Gate(masked, b1, b2, n_pos, n_neg, falsified, verdict, tuple(reasons))


def tie_mass_at_top(scores: list[float]) -> tuple[float, int] | None:
    """The 3rd-highest score and how many jobs share it; ``None`` below 3."""
    if len(scores) < 3:
        return None
    third = sorted(scores, reverse=True)[2]
    return third, sum(1 for s in scores if s == third)


@dataclass(frozen=True)
class CutoffRow:
    """One prune cutoff: what pruning at or below it would remove."""

    cutoff: float
    positives: int
    negatives: int
    lost: float | Undefined
    backlog: int
    backlog_share: float | Undefined


def cutoff_table(
    rows: list[Row], backlog_scores: list[float], cutoffs=PRUNE_CUTOFFS
) -> list[CutoffRow]:
    """Per cutoff, labelled rows with masked prerank ``<= cutoff``, the share of
    all positives that is, and backlog jobs with unmasked prerank ``<= cutoff``.
    A score equal to the cutoff counts as pruned, as ``cli.prune_backlog``
    prunes it."""
    n_pos = sum(r.positive for r in rows)
    out = []
    for cutoff in sorted(cutoffs):
        below = [r for r in rows if r.gate.score <= cutoff]
        pos = sum(r.positive for r in below)
        backlog = sum(1 for s in backlog_scores if s <= cutoff)
        out.append(
            CutoffRow(
                cutoff=cutoff,
                positives=pos,
                negatives=len(below) - pos,
                lost=pos / n_pos if n_pos else Undefined("no positives"),
                backlog=backlog,
                backlog_share=(
                    backlog / len(backlog_scores)
                    if backlog_scores
                    else Undefined("empty backlog")
                ),
            )
        )
    return out


def histogram(scores: list[float], width: int = 10) -> list[tuple[int, int]]:
    """``(bin floor, count)``, highest bin first."""
    counts = Counter(math.floor(s / width) * width for s in scores)
    return sorted(counts.items(), reverse=True)


# ------------------------------------------------------------------ report


def _auc_str(result: Auc | Undefined) -> str:
    if isinstance(result, Undefined):
        return f"undefined ({result.reason})"
    return f"{result.value:.3f}  95% CI [{result.lo:.3f}, {result.hi:.3f}]"


def _num(value: float | Undefined) -> str:
    return (
        f"undefined ({value.reason})"
        if isinstance(value, Undefined)
        else f"{value:.3f}"
    )


def _features(p: Prerank) -> str:
    return " ".join(f"{k}={v:+g}" for k, v in p.features.items() if v)


def header(enforce_geo: bool) -> str:
    wide = os.environ.get("TITLE_FILTER_WIDE")
    title_map = (
        f"TITLE_FILTER_WIDE={wide!r}" if wide is not None else "TITLE_FILTER_WIDE unset"
    )
    return (
        f"prerank v{PRERANK_VERSION} · title map: {title_map} · "
        f"geo enforce in parsed term: {'on' if enforce_geo else 'off'}"
    )


def _pct(value: float | Undefined) -> str:
    return "undefined" if isinstance(value, Undefined) else f"{value:.1%}"


def report(corpus: Corpus, *, half: str | None, cutoffs=PRUNE_CUTOFFS) -> Gate:
    """Print one user's report. Pure output."""
    rows = corpus.rows
    n_j = sum(r.source == JOBS for r in rows)
    print(f"\n── users/{corpus.user_id} " + "─" * 40)
    print(
        f"   {len(rows)} labelled row(s): {n_j} scored job doc(s), "
        f"{len(rows) - n_j} tombstone(s)" + (f"   (half: {half})" if half else "")
    )
    if not corpus.has_preferences:
        print("   No usable preferences: every profile-dependent term is 0.")
    print(f"   residence country: {corpus.residence or 'none'}")

    g = gate(rows)
    print(f"\n   1. Gate (mask: {', '.join(sorted(GATE_MASK))}; discovered_at unused)")
    print(f"     prerank (masked)      {_auc_str(g.masked)}")
    print(f"     B1 content-blind      {_auc_str(g.b1)}")
    print(f"     B2 title_exact alone  {_auc_str(g.b2)}")
    print(
        f"     {g.n_pos} positive / {g.n_neg} negative (>= {QUEUE_DEFAULT_MIN_SCORE})"
    )
    print(
        f"     VERDICT: {g.verdict}"
        + (f" — {'; '.join(g.reasons)}" if g.reasons else "")
    )

    print(
        "\n   2. Per feature, masked  (* = J-only input, read unmasked, leaks the label)"
    )
    for name, stat in feature_stats(rows).items():
        mark = "*" if name in GATE_MASK else " "
        print(
            f"     {name:<14}{mark} fires {stat.fires:6.1%}  lift {_num(stat.lift):<8}"
            f"  AUC {_auc_str(stat.auc)}"
        )
    sig = location_signal_stats(rows)
    print(
        f"     location      * signal only, NO WEIGHT: foreign on {sig.overall:.1%} "
        f"of rows, {sig.positives:.1%} of positives"
    )

    falsified = location_falsifications(rows)
    print(f"\n   3. Location falsifications: {len(falsified)}")
    for r in falsified:
        print(
            f"     {r.job_id}  Pro {r.outcome:g}  {r.doc.get('title')!r} @ "
            f"{r.doc.get('company')!r}  location {r.doc.get('location')!r}  "
            f"[{_features(r.full)}]"
        )
    foreign = foreign_positives(rows)
    print(
        f"     diagnostic, does not gate: {len(foreign)} positive(s) carry the "
        f"foreign location signal"
        + (f"; up to {FOREIGN_LIST_LIMIT} listed by id" if foreign else "")
    )
    for r in foreign[:FOREIGN_LIST_LIMIT]:
        print(
            f"     {r.job_id}  Pro {r.outcome:g}  {r.doc.get('title')!r} @ "
            f"{r.doc.get('company')!r}  location {r.doc.get('location')!r}"
        )

    buried = false_buries(rows)
    print(f"\n   4. False buries (positives below the masked median): {len(buried)}")
    for r in buried:
        print(
            f"     {r.job_id}  prerank {r.gate.score:+g}  Pro {r.outcome:g}  "
            f"{r.doc.get('title')!r} @ {r.doc.get('company')!r}  [{_features(r.gate)}]"
        )

    labels = [r.positive for r in rows]
    top = precision_at_top([r.gate.score for r in rows], labels)
    base = sum(labels) / len(labels) if labels else Undefined("no rows")
    print("\n   5. Top-decile precision (masked, ties fractional)")
    print(f"     {_num(top)}  vs base rate {_num(base)}")

    b = corpus.backlog
    print(f"\n   6. Backlog: {len(b.scores)} pending unscored job(s), unmasked prerank")
    for floor, count in histogram(b.scores):
        print(f"     [{floor:+5d}, {floor + 10:+5d})  {count}")
    tie = tie_mass_at_top(b.scores)
    if tie is None:
        print("     tie mass at the top: undefined (fewer than 3 jobs)")
    else:
        print(
            f"     tie mass at the top: {tie[1]} job(s) share the 3rd-highest score {tie[0]:+g}"
        )
    print(
        f"     foreign location signal on {b.foreign_signals} (no weight); "
        f"{b.with_parse} carry jd_parsed"
    )
    if b.cache_hits is not None:
        print(
            f"     jd_cache: {b.cache_hits} of {b.unparsed} unparsed hit; "
            f"the prefilter would reject {b.cache_rejects} of those"
        )

    print("\n   7. Prune cutoffs (prune = prerank <= cutoff)")
    print(
        "     labelled: masked prerank; backlog: unmasked, as prune_backlog "
        "scores it\n     (the backlog also carries the parsed term the mask "
        f"hides; a prefilter reject sits\n     at {W_PARSED_REJECT:+g})"
    )
    print(
        f"     {'cutoff':>7}{'pos <=':>9}{'neg <=':>9}{'pos lost':>10}"
        f"{'backlog <=':>12}{'of backlog':>12}"
    )
    for row in cutoff_table(rows, b.scores, cutoffs):
        print(
            f"     {row.cutoff:>+7g}{row.positives:>9}{row.negatives:>9}"
            f"{_pct(row.lost):>10}{row.backlog:>12}{_pct(row.backlog_share):>12}"
        )
    return g


def report_pooled(corpora: list[Corpus]) -> None:
    """Within-user pooled AUC. Never one global ranking."""
    per_user = [split_scores(c.rows, lambda r: r.gate.score) for c in corpora]
    pairs = sum(len(f) * len(u) for f, u in per_user)
    print("\n── pooled, within-user pairs only " + "─" * 26)
    print(f"   masked AUC {_num(pooled_auc(per_user))} over {pairs} pair(s)")


# -------------------------------------------------------------------- main


async def run(
    db,
    user_ids: list[str],
    *,
    half: str | None,
    enforce_geo: bool,
    with_cache: bool,
    cutoffs=PRUNE_CUTOFFS,
) -> list[Corpus]:
    print(header(enforce_geo))
    corpora: list[Corpus] = []
    for user_id in user_ids:
        corpus = await load_user(
            db, user_id, half=half, enforce_geo=enforce_geo, with_cache=with_cache
        )
        if corpus is None:
            print(f"\n── users/{user_id}: no such user, skipped")
            continue
        report(corpus, half=half, cutoffs=cutoffs)
        corpora.append(corpus)
    if len(corpora) > 1:
        report_pooled(corpora)
    return corpora


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", action="append", required=True)
    parser.add_argument(
        "--half",
        choices=("even", "odd"),
        default=None,
        help="Restrict the labelled corpus to one sha256(id)-parity half",
    )
    parser.add_argument(
        "--with-cache",
        action="store_true",
        help="Also count jd_cache hits on the backlog (reads every pending jd_raw)",
    )
    parser.add_argument(
        "--cutoff",
        type=float,
        action="append",
        default=None,
        help="A prune cutoff for section 7 (repeatable; replaces the defaults)",
    )
    args = parser.parse_args()
    bind_run_context("prerank_eval", user_id=",".join(args.user_id))
    await run(
        firestore_client(),
        args.user_id,
        half=args.half,
        enforce_geo=geo_enforce_enabled(),
        with_cache=args.with_cache,
        cutoffs=tuple(args.cutoff) if args.cutoff else PRUNE_CUTOFFS,
    )


if __name__ == "__main__":
    asyncio.run(main())
