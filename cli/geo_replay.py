# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Replay ``tools.matching.geo`` over already-scored history and measure it.

READ-ONLY, and free: this streams Firestore, writes nothing, and calls no
model. It exists because the geo gate is only worth shipping if it is provably
safe, and "provably" here means one number — the false-positive rate against
decisions Pro already made and was paid for.

The corpus is every job doc carrying **both** ``jd_parsed`` and ``match``.
Those docs all survived ``score.persist_result``, which tombstones anything at
or below 20 — so with rare exceptions (docs the user acted on before the
discard rule existed) *Pro judged every one of them eligible*. That makes the
arithmetic unusually clean:

- **false positive** — the gate says ``ineligible``, Pro scored above 20. A job
  the user would have been shown and now never sees. This is the number that
  decides whether the gate ships.
- **true positive** — the gate says ``ineligible`` and Pro also capped at
  exactly 20. A Pro call we could have skipped.
- **miss** — the gate abstains where Pro capped at 20. Coverage left on the
  table, which costs money but nothing else.

``--with-discarded`` also counts the ``discarded_jobs`` tombstones, which is
where most of the geo rejections actually went. They can only ever be a
denominator: ``score.discard_tombstone`` writes a deliberately minimal record
with a ``score`` and no ``jd_parsed``, so there is nothing to run the gate
against.

Usage:
    python -m cli.geo_replay --user-id me
    python -m cli.geo_replay --user-id me --user-id E3cika... --with-discarded
    python -m cli.geo_replay --all-users
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
from tools.matching import geo
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

        The kept records, plus the tombstones that cost a Pro call — which is
        all of them *except* the out-of-family sentinel, since
        ``pipeline.OUT_OF_FAMILY`` is returned by the pre-filter without any
        call being made. Zero unless ``--with-discarded`` did the counting.
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

    Zero observed failures is the expected outcome here and the case a naive
    ``k/n`` reports as "0%, done" — which is not what a sample of 1,127 can
    support. The rule of three (``3/n``) is the standard answer for that case;
    anything else falls back to a Wilson score bound, which stays sane at the
    small counts this will actually see.
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
            # An old doc whose parse predates a schema change contributes
            # nothing either way; counted so it can't hide a systematic gap.
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
                # pipeline.OUT_OF_FAMILY's sentinel: the family pre-filter
                # returned it without ever calling Pro, so it is free and must
                # not inflate the denominator below.
                result.tombstones_free += 1
    return result


def report(r: Replay, *, title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))
    print(f"   residence={r.residence}   gate v{geo.GATE_VERSION}")
    print(f"   {r.n} record(s) carrying both jd_parsed and match", end="")
    print(f" ({r.unparseable} unparseable, skipped)" if r.unparseable else "")
    if not r.n:
        return

    print(f"\n   {'verdict':<12}{'Pro capped @20':>16}{'Pro kept >20':>14}{'total':>8}")
    for verdict in VERDICTS:
        capped, kept = r.cells[(verdict, True)], r.cells[(verdict, False)]
        total = capped + kept
        print(f"   {verdict:<12}{capped:>16}{kept:>14}{total:>8}  ({total / r.n:6.1%})")

    fp_rate = r.fp / r.n
    print(
        f"\n   false positives : {r.fp:5d}  = {fp_rate:.2%} of n"
        f"   (95% upper bound {upper_bound_95(r.fp, r.n):.2f}%)"
    )
    print(f"   true positives  : {r.tp:5d}  Pro also capped these at 20")
    print(f"   missed          : {r.missed:5d}  Pro capped at 20, the gate abstained")
    print(
        f"\n   projected Pro-call reduction: {r.ineligible}/{r.n} = "
        f"{r.ineligible / r.n:.1%} of these records"
    )

    if r.tp + r.missed == 0:
        # The expected outcome, and it must not be misread as a broken gate.
        # `persist_result` tombstones everything at or below 20, so the geo
        # rejections are exactly the records that are NOT here. This corpus
        # can therefore falsify the gate (any ineligible verdict in it is a
        # false positive) but can never confirm its coverage — the reduction
        # figure above is a floor of zero, not a measurement.
        print(
            "   ^ this corpus is survivorship-biased: every geo rejection was\n"
            "     tombstoned out of `jobs`, so 0 is the expected reduction here\n"
            "     and the FP rate above is the only number this run measures."
        )

    print("\n   rule fired:")
    for rule, count in r.rules.most_common():
        print(f"     {count:6d}  {rule}")

    if r.tombstones:
        # The other half of the denominator — counts only, since a tombstone
        # carries a score and no jd_parsed. This is where the gate's actual
        # upside lives, and the one place its size can be seen.
        print(
            f"\n   discarded_jobs tombstones: {r.tombstones}"
            f" ({r.tombstones_free} scored 0 = out-of-family, never a Pro call)"
        )
        print(
            f"   Pro calls in this history: {r.pro_calls}, of which "
            f"{r.tombstones_capped} ({r.tombstones_capped / r.pro_calls:.1%}) were "
            "capped at exactly 20"
        )
        print(
            "   That share is the ceiling on what this gate can save, and none "
            "of it\n   is replayable — those docs have no jd_parsed to run the "
            "gate against."
        )

    if r.false_positives:
        print(f"\n   ── every false positive ({len(r.false_positives)}) ──")
        for line in r.false_positives:
            print(line)


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
    args = parser.parse_args()
    if not args.user_ids and not args.all_users:
        parser.error("pass --user-id (repeatable) or --all-users")
    bind_run_context("geo_replay", user_id=",".join(args.user_ids) or "all")

    db = firestore.AsyncClient()
    user_ids = list(args.user_ids)
    if args.all_users:
        async for snap in db.collection("users").stream():
            if snap.id not in user_ids:
                user_ids.append(snap.id)

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
        users=reports,
        n=combined.n,
        false_positives=combined.fp,
        gate_version=geo.GATE_VERSION,
    )


if __name__ == "__main__":
    asyncio.run(main())
