# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Replay ``tools.matching.geo`` over already-scored history and measure it.

READ-ONLY and free: streams Firestore, writes nothing, calls no model. It
exists to produce one number — the gate's false-positive rate against
decisions Pro already made and was paid for.

The default corpus is every job doc carrying both ``jd_parsed`` and ``match``.
Those all survived ``score.persist_result``, which tombstones anything at or
below 20, so Pro judged essentially every one of them eligible:

- **false positive** — the gate says ``ineligible``, Pro scored above 20. A job
  the user would have been shown and now never sees. This decides whether the
  gate ships.
- **true positive** — the gate says ``ineligible`` and Pro also capped at
  exactly 20. A Pro call we could have skipped.
- **miss** — the gate abstains where Pro capped at 20. Coverage left on the
  table, which costs money and nothing else.

``--with-discarded`` also counts the ``discarded_jobs`` tombstones, where most
geo rejections went. They are counted as a denominator only.

``--corpus jd-cache`` answers the question the per-user ``jobs`` corpus
structurally cannot. That corpus is survivorship-filtered — every job the gate
would have caught was tombstoned out of it — so it can falsify the gate but can
never show that the gate fires at all. The cross-user ``jd_cache`` collection
caches parses the moment Flash produces one, regardless of what happened to the
job afterwards, so it is the closest thing to an unfiltered sample. It reports
a distribution, not a contingency table: a cache doc carries no ``match``, so
there is no false-positive rate to compute there.

``--readiness`` answers a different question and does not replay anything: it
counts the ``geo_gate`` records already written to job docs and tombstones by
``score.shadow_geo_gate``, which are what the gate said at scoring time, under
whatever ``GATE_VERSION`` was live then. The replay recomputes today's gate
over today's parses, so the two can disagree after a version bump or a parse
schema change, and neither is a substitute for the other.

Usage:
    python -m cli.geo_replay --user-id me
    python -m cli.geo_replay --user-id me --user-id E3cika... --with-discarded
    python -m cli.geo_replay --all-users
    python -m cli.geo_replay --corpus jd-cache --user-id E3cika...
    python -m cli.geo_replay --user-id me --readiness
"""

from __future__ import annotations

import argparse
import asyncio
import math
from collections import Counter
from dataclasses import dataclass, field

from dotenv import load_dotenv
from google.cloud import firestore

from models.job import ParsedJD
from models.profile import MasterProfile
from obs.logging import bind_run_context, get_logger
from tools.matching import geo, jd_cache, rates
from tools.matching.score import DISCARD_AT_OR_BELOW

load_dotenv()

log = get_logger("cli.geo_replay")

# Pro signals "geographically ineligible" by capping at exactly this, which is
# also the discard threshold — see Rule 6 in pipeline.MATCH_CONTEXT_TEMPLATE.
# Compared exactly rather than with <=, because a weighted score that merely
# lands under the threshold is a bad match, not a geo rejection.
GEO_CAP_SCORE = float(DISCARD_AT_OR_BELOW)

VERDICTS = ("ineligible", "eligible", "abstain")


@dataclass
class Replay:
    """One user's contingency table, plus the false positives to hand-read."""

    user_id: str
    residence: str | None = None
    n: int = 0
    unparseable: int = 0
    # (verdict, Pro capped at 20?) -> count. The whole report derives from it.
    cells: Counter = field(default_factory=Counter)
    rules: Counter = field(default_factory=Counter)
    false_positives: list[str] = field(default_factory=list)
    tombstones: int = 0
    tombstones_capped: int = 0
    tombstones_free: int = 0

    def record(self, decision: geo.GeoDecision, capped: bool) -> None:
        self.n += 1
        self.cells[(decision.verdict, capped)] += 1
        self.rules[decision.rule] += 1

    @property
    def fp(self) -> int:
        return self.cells[("ineligible", False)]

    @property
    def tp(self) -> int:
        return self.cells[("ineligible", True)]

    @property
    def missed(self) -> int:
        return self.cells[("abstain", True)]

    @property
    def ineligible(self) -> int:
        return self.tp + self.fp

    @property
    def pro_calls(self) -> int:
        """Every Pro call this user's history represents.

        The kept records plus every tombstone except the out-of-family
        sentinel, which the pre-filter returns without calling Pro. Zero unless
        ``--with-discarded`` did the counting.
        """
        return self.n + self.tombstones - self.tombstones_free

    def merge(self, other: Replay) -> None:
        self.n += other.n
        self.unparseable += other.unparseable
        self.cells.update(other.cells)
        self.rules.update(other.rules)
        self.false_positives.extend(other.false_positives)
        self.tombstones += other.tombstones
        self.tombstones_capped += other.tombstones_capped
        self.tombstones_free += other.tombstones_free


def upper_bound_95(k: int, n: int) -> float:
    """95% one-sided upper bound on a rate, as a percentage.

    Zero observed failures is the expected outcome, and a naive ``k/n`` reports
    that as "0%", which no finite sample supports. ``k == 0`` uses the rule of
    three; anything else uses a Wilson score bound, which stays sane at small
    counts.
    """
    if n == 0:
        return 100.0
    if k == 0:
        return 300.0 / n  # rule of three, as a percentage
    z = 1.96
    p = k / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return min(center + half, 1.0) * 100.0


def _detail(doc: dict, parsed: ParsedJD, match: dict, d: geo.GeoDecision) -> str:
    """Everything needed to judge one false positive by hand, in two lines."""
    return (
        f"    {match.get('overall_score')!s:>5}  {match.get('recommendation', '?'):12} "
        f"{doc.get('company', '?')} - {str(doc.get('title', '?'))[:48]}\n"
        f"          rule={d.rule} residence={d.residence_country} "
        f"job_country={parsed.job_country!r} state={parsed.job_state!r} "
        f"remote_policy={parsed.remote_policy!r} scope={parsed.remote_scope!r} "
        f"us_remote_ok={parsed.us_remote_ok} location={doc.get('location')!r}"
    )


async def replay_user(
    db: firestore.AsyncClient, user_id: str, *, with_discarded: bool
) -> Replay | None:
    """Run the gate over one user's scored history. ``None`` = no profile."""
    result = Replay(user_id=user_id)
    snap = await db.collection("users").document(user_id).get()
    if not snap.exists:
        return None
    try:
        profile = MasterProfile.model_validate(snap.to_dict())
    except Exception as e:
        print(f"  ! users/{user_id}: unreadable profile ({str(e)[:80]}) — skipped")
        return None
    result.residence = geo.normalize_country(
        profile.residence.country if profile.residence else None
    )

    user_ref = db.collection("users").document(user_id)
    async for job_snap in user_ref.collection("jobs").stream():
        doc = job_snap.to_dict() or {}
        match = doc.get("match")
        if not doc.get("jd_parsed") or not match:
            continue
        try:
            parsed = ParsedJD.model_validate(doc["jd_parsed"])
        except Exception:
            # A parse predating a schema change contributes nothing either
            # way; counted so it can't hide a systematic gap.
            result.unparseable += 1
            continue
        decision = geo.evaluate(parsed, profile)
        capped = float(match.get("overall_score", -1)) == GEO_CAP_SCORE
        result.record(decision, capped)
        if decision.verdict == "ineligible" and not capped:
            result.false_positives.append(_detail(doc, parsed, match, decision))

    if with_discarded:
        async for tomb in user_ref.collection("discarded_jobs").stream():
            result.tombstones += 1
            score = float((tomb.to_dict() or {}).get("score", -1))
            if score == GEO_CAP_SCORE:
                result.tombstones_capped += 1
            elif score == 0.0:
                # pipeline.OUT_OF_FAMILY's sentinel: no Pro call was made, so
                # it must not inflate the denominator below.
                result.tombstones_free += 1
    return result


@dataclass
class CacheReplay:
    """The gate's verdict distribution over the cross-user parse cache.

    Deliberately not a :class:`Replay`: a cache doc is a parse and nothing
    else, so there is no ``capped`` axis, and reusing the contingency table
    would print a false-positive rate of zero over a corpus that cannot measure
    one.
    """

    user_id: str
    residence: str | None = None
    n: int = 0
    unparseable: int = 0
    verdicts: Counter = field(default_factory=Counter)
    rules: Counter = field(default_factory=Counter)

    def record(self, decision: geo.GeoDecision) -> None:
        self.n += 1
        self.verdicts[decision.verdict] += 1
        self.rules[decision.rule] += 1


async def replay_jd_cache(
    db: firestore.AsyncClient, user_id: str
) -> CacheReplay | None:
    """Run the gate over every cached parse. ``None`` = no usable profile.

    ``--user-id`` does not select the corpus — the corpus is every cached
    parse. It selects the residence the corpus is evaluated against, since
    ``geo.evaluate`` reads ``residence.country`` and nothing else.
    """
    result = CacheReplay(user_id=user_id)
    snap = await db.collection("users").document(user_id).get()
    if not snap.exists:
        print(f"  ! users/{user_id}: no such user — nothing to evaluate against")
        return None
    try:
        profile = MasterProfile.model_validate(snap.to_dict())
    except Exception as e:
        print(f"  ! users/{user_id}: unreadable profile ({str(e)[:80]}) — skipped")
        return None
    result.residence = geo.normalize_country(
        profile.residence.country if profile.residence else None
    )

    async for doc_snap in db.collection(jd_cache.COLLECTION).stream():
        doc = doc_snap.to_dict() or {}
        try:
            parsed = ParsedJD.model_validate(doc.get("jd_parsed"))
        except Exception:
            # Schema drift, as ``jd_cache.lookup_many`` treats it: a doc the
            # current model can't read is a miss, not a verdict.
            result.unparseable += 1
            continue
        result.record(geo.evaluate(parsed, profile))
    return result


def report_jd_cache(r: CacheReplay) -> None:
    print(f"\n── jd_cache vs users/{r.user_id} " + "─" * 30)
    print(f"   residence={r.residence}   gate v{geo.GATE_VERSION}")
    print(f"   {r.n} cached parse(s)", end="")
    print(f" ({r.unparseable} unparseable, skipped)" if r.unparseable else "")
    if not r.n:
        return

    print(f"\n   {'verdict':<12}{'count':>10}{'share':>10}")
    for verdict in VERDICTS:
        count = r.verdicts[verdict]
        print(f"   {verdict:<12}{count:>10}{count / r.n:>10.1%}")

    print("\n   rule fired:")
    for rule, count in r.rules.most_common():
        print(f"     {count:6d}  {rule}  ({count / r.n:.1%})")

    print(
        "\n   This corpus carries no Pro decisions, so nothing here is a\n"
        "   false-positive rate — that number comes from the `jobs` corpus.\n"
        "   What it does show is that the gate is not silent: the ineligible\n"
        "   share above is the fraction of *all* parses it would act on."
    )


# ------------------------------------------------------------- readiness
#
# A different question from the replay above, over different inputs: what did
# the gate *actually say*, at the version that was live, on records Pro also
# judged. Nothing below re-runs a rule.

#: Collections carrying ``score.persist_result``'s ``geo_gate`` records, in
#: the order the report prints them.
SHADOW_SOURCES = ("jobs", "discarded_jobs")


def _pct(k: int, n: int) -> str:
    """``k/n`` as a percentage, or ``"undefined"`` when ``n`` is zero.

    An account whose every record was a free reject really does have no
    denominator, and printing ``0.0%`` there invents a measurement.
    """
    return f"{k / n:.1%}" if n else "undefined"


def _shadow_detail(doc: dict, rec: dict) -> str:
    """One recorded false positive, in the two lines needed to judge it."""
    return (
        f"    {rec.get('pro_score')!s:>5}  {doc.get('company', '?')} - "
        f"{str(doc.get('title', '?'))[:48]}\n"
        f"          rule={rec.get('rule')} v{rec.get('version')} "
        f"residence={rec.get('residence_country')!r} "
        f"job_country={rec.get('job_country')!r} "
        f"pro_geo_flag={rec.get('pro_geo_flag')}"
    )


@dataclass
class ShadowTally:
    """Recorded ``geo_gate`` verdicts from one collection, split by what Pro
    then did with the same job.

    Enforced records are set aside rather than counted: the gate skipped the
    Pro call, so there is no Pro verdict to agree or disagree with, and
    folding them in would let the gate grade its own homework.
    """

    source: str
    docs: int = 0
    no_record: int = 0
    enforced: int = 0
    verdicts: Counter = field(default_factory=Counter)
    rules: Counter = field(default_factory=Counter)
    versions: Counter = field(default_factory=Counter)
    # ``ineligible``, split by Pro's score.
    tp: int = 0  # Pro capped at exactly 20 — that Pro call bought nothing
    fp: int = 0  # Pro scored above 20 — a live job the gate would dismiss
    moot: int = 0  # Pro discarded it on fit, so the gate would cost nothing
    # ``eligible``, split the same way.
    cleared: int = 0  # Pro did not cap either — both say reachable
    cleared_capped: int = 0  # Pro capped at 20 — the gate cleared it wrongly
    # ``abstain`` where Pro capped at 20: coverage left on the table.
    missed: int = 0
    false_positives: list[str] = field(default_factory=list)

    def record(self, rec: dict, doc: dict) -> None:
        self.versions[rec.get("version")] += 1
        if rec.get("enforced") or rec.get("pro_score") is None:
            self.enforced += 1
            return
        verdict = str(rec.get("verdict", "?"))
        self.verdicts[verdict] += 1
        self.rules[str(rec.get("rule", "?"))] += 1
        score = float(rec.get("pro_score") or 0.0)
        capped = score == GEO_CAP_SCORE
        if verdict == "ineligible":
            if capped:
                self.tp += 1
            elif score > GEO_CAP_SCORE:
                self.fp += 1
                self.false_positives.append(_shadow_detail(doc, rec))
            else:
                self.moot += 1
        elif verdict == "eligible":
            if capped:
                self.cleared_capped += 1
            else:
                self.cleared += 1
        elif capped:
            self.missed += 1

    @property
    def shadow(self) -> int:
        """Verdicts with a Pro decision beside them — the real denominator."""
        return sum(self.verdicts.values())

    @property
    def claims(self) -> int:
        """Verdicts that assert something. ``abstain`` asserts nothing."""
        return self.verdicts["ineligible"] + self.verdicts["eligible"]

    @property
    def agree(self) -> int:
        return self.tp + self.cleared

    @property
    def avoidable(self) -> int:
        """Pro calls enforcement would have skipped, false positives and all."""
        return self.verdicts["ineligible"]

    def merge(self, other: ShadowTally) -> None:
        self.docs += other.docs
        self.no_record += other.no_record
        self.enforced += other.enforced
        self.verdicts.update(other.verdicts)
        self.rules.update(other.rules)
        self.versions.update(other.versions)
        self.tp += other.tp
        self.fp += other.fp
        self.moot += other.moot
        self.cleared += other.cleared
        self.cleared_capped += other.cleared_capped
        self.missed += other.missed
        self.false_positives.extend(other.false_positives)


async def readiness_user(
    db: firestore.AsyncClient, user_id: str
) -> dict[str, ShadowTally] | None:
    """Count the ``geo_gate`` records on one user's jobs and tombstones.

    Reads no profile and evaluates no rule, so a user whose residence has
    since changed — or whose profile no longer validates — is still countable.
    ``None`` means no such user.
    """
    user_ref = db.collection("users").document(user_id)
    snap = await user_ref.get()
    if not snap.exists:
        print(f"  ! users/{user_id}: no such user")
        return None
    tallies = {source: ShadowTally(source=source) for source in SHADOW_SOURCES}
    for source in SHADOW_SOURCES:
        tally = tallies[source]
        async for doc_snap in user_ref.collection(source).stream():
            doc = doc_snap.to_dict() or {}
            tally.docs += 1
            rec = doc.get("geo_gate")
            if isinstance(rec, dict):
                tally.record(rec, doc)
            else:
                tally.no_record += 1
    return tallies


def report_readiness(tallies: dict[str, ShadowTally], *, title: str) -> None:
    """Print the go/no-go counts for ``GEO_GATE_ENFORCE``, and nothing else.

    Deliberately states no recommendation: whether this is grounds to flip the
    flag is the operator's call, and a tool that answers it would be read
    instead of the numbers.
    """
    jobs, tombs = tallies["jobs"], tallies["discarded_jobs"]
    total = ShadowTally(source="total")
    total.merge(jobs)
    total.merge(tombs)

    print(f"\n── GEO_GATE_ENFORCE readiness: {title} " + "─" * 20)
    print(
        "   Counts the geo_gate records score.shadow_geo_gate wrote when each\n"
        "   job was scored, under whatever GATE_VERSION was live then. This is\n"
        f"   not a replay — `--corpus jobs` recomputes today's v{geo.GATE_VERSION} "
        "instead, and\n   the two may disagree after a version or parse-schema change."
    )

    print(
        f"\n   {'source':<16}{'docs':>8}{'shadow verdicts':>18}"
        f"{'no record':>12}{'gate-enforced':>15}"
    )
    for t in (jobs, tombs, total):
        print(
            f"   {t.source:<16}{t.docs:>8}{t.shadow:>18}"
            f"{t.no_record:>12}{t.enforced:>15}"
        )
    if total.versions:
        seen = ", ".join(f"v{v} x{c}" for v, c in sorted(total.versions.items()))
        print(f"   gate versions behind those records: {seen}")

    if not total.shadow:
        # Real, not hypothetical: an account scored entirely before shadow
        # recording shipped has docs and no verdicts at all.
        print(
            "\n   No shadow verdicts recorded, so there is nothing to compare\n"
            "   against Pro and no rate to quote. Every figure below would be\n"
            "   undefined, so none is printed."
        )
        return

    print(
        f"\n   {'verdict':<14}{'jobs':>8}{'discarded_jobs':>17}{'total':>8}{'share':>11}"
    )
    for verdict in VERDICTS:
        n = total.verdicts[verdict]
        print(
            f"   {verdict:<14}{jobs.verdicts[verdict]:>8}"
            f"{tombs.verdicts[verdict]:>17}{n:>8}{_pct(n, total.shadow):>11}"
        )

    print(
        f"\n   agreement with Pro, over the {total.claims} verdict(s) where the "
        "gate made\n   a claim (abstain makes none, so it is not in the "
        "denominator):"
    )
    print(
        f"     agree          : {total.agree:5d}  = {_pct(total.agree, total.claims)}"
        f"   ({total.tp} ineligible+Pro capped, {total.cleared} eligible+Pro kept)"
    )
    print(
        f"     false positives: {total.fp:5d}  = {_pct(total.fp, total.claims)}"
        "   gate ineligible, Pro scored above 20"
    )
    print(
        f"     moot           : {total.moot:5d}"
        "          gate ineligible, Pro discarded it on fit anyway"
    )
    print(
        f"     wrongly cleared: {total.cleared_capped:5d}"
        "          gate eligible, Pro capped at 20"
    )
    print(
        f"   plus {total.verdicts['abstain']} abstain(s), {total.missed} of them on "
        "jobs Pro capped at 20 —\n   coverage the gate leaves on the table, which "
        "costs money and nothing else."
    )

    print("\n   false positives — the number that decides the flag")
    print(
        f"     jobs           : {jobs.fp} of {jobs.shadow} = "
        f"{_pct(jobs.fp, jobs.shadow)}, 95% upper bound "
        f"{upper_bound_95(jobs.fp, jobs.shadow):.2f}%"
    )
    print(
        f"     discarded_jobs : not measurable ({tombs.fp} found, and more than 0\n"
        "                      would be a bug) — every record there scored 20 or\n"
        "                      less, so Pro kept none of them"
    )
    print(
        "   Quote that rate off `jobs` only. It is survivorship-filtered —\n"
        "   persist_result tombstones everything at or below 20 — so it can\n"
        "   falsify the gate but can never show how often the gate fires; the\n"
        "   verdict table above and `--corpus jd-cache` are where that lives."
    )

    saved_usd = total.avoidable * rates.MEASURED_SCORE_USD
    rated_usd = total.avoidable * rates.MEASURED_RATED_JOB_USD
    print(
        f"\n   avoidable Pro calls: {total.avoidable} ineligible verdict(s) of "
        f"{total.shadow} shadow\n   record(s) = {_pct(total.avoidable, total.shadow)}"
        f", at ${rates.MEASURED_SCORE_USD:.5f} per score leg\n"
        f"   (rates.MEASURED_SCORE_USD, measured {rates.MEASURED_AT}) = "
        f"${saved_usd:.2f} of Pro\n   scoring this history need not have bought."
    )
    print(
        f"   The score leg, not the ${rates.MEASURED_RATED_JOB_USD:.5f} rated-job "
        "rate: pipeline.prefilter\n   runs on jd_parsed, so the parse leg is already "
        "paid by the time the gate\n   can fire. The blended "
        f"${rates.MEASURED_COST_PER_JOB_USD:.5f} per job *attempted* is not used "
        "at all —\n   it averages in free pre-filter rejects and understates a job "
        f"that reached\n   Pro. Those same jobs cost ${rated_usd:.2f} as fully rated "
        "jobs, which is what\n   the saving would be if the gate ever ran ahead of "
        "the parse."
    )
    print(
        f"   Includes the {total.fp} false positive(s) above, which are a cost, "
        "not a saving."
    )

    if total.false_positives:
        print(f"\n   ── every recorded false positive ({total.fp}) ──")
        for line in total.false_positives:
            print(line)


def report(r: Replay, *, title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))
    print(f"   residence={r.residence}   gate v{geo.GATE_VERSION}")
    print(f"   {r.n} record(s) carrying both jd_parsed and match", end="")
    print(f" ({r.unparseable} unparseable, skipped)" if r.unparseable else "")
    # Every figure in this block divides by ``r.n``. An account whose kept
    # jobs were all tombstoned has none, and still has a tombstone census
    # worth printing below.
    if r.n:
        print(
            f"\n   {'verdict':<12}{'Pro capped @20':>16}{'Pro kept >20':>14}{'total':>8}"
        )
        for verdict in VERDICTS:
            capped, kept = r.cells[(verdict, True)], r.cells[(verdict, False)]
            total = capped + kept
            print(
                f"   {verdict:<12}{capped:>16}{kept:>14}{total:>8}"
                f"  ({total / r.n:6.1%})"
            )

        fp_rate = r.fp / r.n
        print(
            f"\n   false positives : {r.fp:5d}  = {fp_rate:.2%} of n"
            f"   (95% upper bound {upper_bound_95(r.fp, r.n):.2f}%)"
        )
        print(f"   true positives  : {r.tp:5d}  Pro also capped these at 20")
        print(
            f"   missed          : {r.missed:5d}  Pro capped at 20, the gate abstained"
        )
        print(
            f"\n   projected Pro-call reduction: {r.ineligible}/{r.n} = "
            f"{r.ineligible / r.n:.1%} of these records"
        )

        if r.tp + r.missed == 0:
            # Expected, not a broken gate: `persist_result` tombstones
            # everything at or below 20, so the geo rejections are the records
            # that are NOT here. The reduction figure above is a floor, not a
            # measurement.
            print(
                "   ^ this corpus is survivorship-biased: every geo rejection was\n"
                "     tombstoned out of `jobs`, so 0 is the expected reduction here\n"
                "     and the FP rate above is the only number this run measures."
            )

        print("\n   rule fired:")
        for rule, count in r.rules.most_common():
            print(f"     {count:6d}  {rule}")

    if r.tombstones:
        # The other half of the denominator, counted rather than replayed.
        # This is where the gate's upside lives and the one place its size
        # shows.
        print(
            f"\n   discarded_jobs tombstones: {r.tombstones}"
            f" ({r.tombstones_free} scored 0 = out-of-family, never a Pro call)"
        )
        if r.pro_calls:
            print(
                f"   Pro calls in this history: {r.pro_calls}, of which "
                f"{r.tombstones_capped} "
                f"({r.tombstones_capped / r.pro_calls:.1%}) were capped at "
                "exactly 20"
            )
            print(
                "   That share is the ceiling on what this gate can save. "
                "Tombstones do\n   carry jd_parsed, so that ceiling is "
                "replayable; this run counts them\n   rather than replaying "
                "them."
            )
        else:
            # Every record here was a free out-of-family reject, so there is
            # no denominator and no share to report.
            print(
                "   Pro calls in this history: 0 — every record was a free "
                "out-of-family\n   reject, so there is no saving for this "
                "gate to measure here."
            )

    if r.false_positives:
        print(f"\n   ── every false positive ({len(r.false_positives)}) ──")
        for line in r.false_positives:
            print(line)


async def _resolve_users(
    db: firestore.AsyncClient, user_ids: list[str], *, all_users: bool
) -> list[str]:
    """The explicit ``--user-id``s first, then every other user if asked."""
    resolved = list(user_ids)
    if all_users:
        async for snap in db.collection("users").stream():
            if snap.id not in resolved:
                resolved.append(snap.id)
    return resolved


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--user-id",
        action="append",
        dest="user_ids",
        default=[],
        help="User to replay; repeatable",
    )
    parser.add_argument(
        "--all-users", action="store_true", help="Replay every user in Firestore"
    )
    parser.add_argument(
        "--with-discarded",
        action="store_true",
        help="Also count discarded_jobs tombstones (slow; large collection)",
    )
    parser.add_argument(
        "--readiness",
        action="store_true",
        help=(
            "Count the recorded shadow verdicts on job docs and tombstones "
            "and print the GEO_GATE_ENFORCE go/no-go numbers. Replays nothing."
        ),
    )
    parser.add_argument(
        "--corpus",
        choices=("jobs", "jd-cache"),
        default="jobs",
        help=(
            "jobs: per-user scored history (measures false positives). "
            "jd-cache: the cross-user parse cache (measures how often the gate "
            "fires at all). Needs exactly one --user-id, for its residence."
        ),
    )
    args = parser.parse_args()
    if not args.user_ids and not args.all_users:
        parser.error("pass --user-id (repeatable) or --all-users")

    if args.readiness and args.corpus != "jobs":
        parser.error("--readiness reads recorded verdicts; it takes no --corpus")

    if args.readiness:
        bind_run_context("geo_replay", user_id=",".join(args.user_ids) or "all")
        db = firestore.AsyncClient()
        user_ids = await _resolve_users(db, args.user_ids, all_users=args.all_users)
        combined = {s: ShadowTally(source=s) for s in SHADOW_SOURCES}
        reports = 0
        for user_id in user_ids:
            tallies = await readiness_user(db, user_id)
            if tallies is None:
                continue
            report_readiness(tallies, title=f"users/{user_id}")
            for source, tally in tallies.items():
                combined[source].merge(tally)
            reports += 1
        if reports > 1:
            report_readiness(combined, title=f"ALL {reports} users")
        shadow = sum(t.shadow for t in combined.values())
        log.info(
            "geo_replay.done",
            corpus="shadow",
            users=reports,
            shadow_verdicts=shadow,
            false_positives=sum(t.fp for t in combined.values()),
            avoidable=sum(t.avoidable for t in combined.values()),
        )
        return

    if args.corpus == "jd-cache":
        # One residence, one distribution: several would print the same corpus
        # repeatedly with no sign in the totals that the denominator never
        # changed.
        if args.all_users or len(args.user_ids) != 1:
            parser.error("--corpus jd-cache takes exactly one --user-id")
        bind_run_context("geo_replay", user_id=args.user_ids[0])
        cached = await replay_jd_cache(firestore.AsyncClient(), args.user_ids[0])
        if cached is None:
            return
        report_jd_cache(cached)
        log.info(
            "geo_replay.done",
            corpus="jd-cache",
            n=cached.n,
            unparseable=cached.unparseable,
            gate_version=geo.GATE_VERSION,
            **{f"verdict_{k}": v for k, v in cached.verdicts.items()},
        )
        return

    bind_run_context("geo_replay", user_id=",".join(args.user_ids) or "all")

    db = firestore.AsyncClient()
    user_ids = await _resolve_users(db, args.user_ids, all_users=args.all_users)

    combined = Replay(user_id="ALL")
    reports = 0
    for user_id in user_ids:
        result = await replay_user(db, user_id, with_discarded=args.with_discarded)
        if result is None:
            continue
        report(result, title=f"users/{user_id}")
        combined.merge(result)
        reports += 1

    if reports > 1:
        combined.residence = "mixed"
        report(combined, title=f"ALL {reports} users")
    log.info(
        "geo_replay.done",
        corpus="jobs",
        users=reports,
        n=combined.n,
        false_positives=combined.fp,
        gate_version=geo.GATE_VERSION,
    )


if __name__ == "__main__":
    asyncio.run(main())
