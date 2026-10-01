# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Replay ``tools.discovery.title_filter`` over the persisted backlog and measure
the widened map before anyone is subject to it.

READ-ONLY, and free: this streams Firestore, writes nothing, calls no model
and touches no env var. It runs the candidate classifier by passing
``wide=True`` explicitly, so it measures ``TITLE_FILTER_WIDE`` without that
flag being set anywhere.

Two corpora, because neither one alone can answer the question.

**Corpus A — job docs carrying a ``match``.** Pro was paid to judge these and
``score.persist_result`` tombstoned everything at or below the discard
threshold, so the survivors are the jobs Pro thought were worth showing. The
corpus is therefore survivorship-filtered and can only ever *falsify*: any job
the wide map would drop that Pro scored above the threshold is a **false
drop** — a job the user would have seen and now never will. Each one is
printed in full for hand-reading. **The ship bar is zero**, the bar the geo
gate cleared at 0/1,127.

**Corpus B — every job doc, match or not.** This is the 8,882-job backlog the
widening was written for, and it is not survivorship-filtered for *this* gate:
the narrow map drops nothing today, so everything discovery ever persisted is
still in it. It answers the other question — whether the map fires at all, or
ships silently inert — by printing the counterfactual: today's outcome for
every title against the candidate's, and what the currently-unclassified
majority would become.

``--with-discarded`` adds the ``discarded_jobs`` tombstones, which carry a
title and a score. They are the clearest evidence of upside available: a drop
there is a Flash parse and a Pro call the filter would have saved, on a job
Pro went on to reject anyway.

The geo replay's corpus B is the top-level ``jd_cache``; this one cannot use
it. A cache doc is ``{jd_parsed, model, created_at}`` and ``ParsedJD`` has no
``title`` field, so there is nothing there for a title classifier to read.
``users/*/jobs`` is the widest population that carries titles.

Usage:
    python -m cli.title_replay                      # every user
    python -m cli.title_replay --user-id ED3UV...
    python -m cli.title_replay --user-id me --with-discarded --top 40
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, field

from dotenv import load_dotenv
from google.cloud import firestore
from pydantic import ValidationError

from models.profile import JobPreferences
from obs.logging import bind_run_context, get_logger
from tools.discovery.title_filter import (
    OUTCOMES,
    TITLE_FILTER_VERSION,
    evaluate_title,
)
from tools.matching.score import DISCARD_AT_OR_BELOW

load_dotenv()

log = get_logger("cli.title_replay")


@dataclass
class TitleReplay:
    """One user's narrow-vs-wide counterfactual, plus the false drops."""

    user_id: str
    families: list[str] = field(default_factory=list)
    n: int = 0
    no_preferences: bool = False
    # outcome -> count, once under today's map and once under the candidate.
    narrow: Counter = field(default_factory=Counter)
    wide: Counter = field(default_factory=Counter)
    # Only newly-dropped jobs land in these.
    by_family: Counter = field(default_factory=Counter)
    by_title: Counter = field(default_factory=Counter)
    # The match-carrying subset — the only part that can falsify the map.
    scored: int = 0
    false_drops: list[tuple[float, str, str]] = field(default_factory=list)
    true_drops: int = 0
    tombstones: int = 0
    tombstones_dropped: int = 0

    def record(
        self, title: str, preferences: JobPreferences | None, score: float | None
    ) -> None:
        """Classify one title twice and file the difference.

        ``score`` is ``match.overall_score`` when Pro judged this job, and
        ``None`` when it never got that far.
        """
        self.n += 1
        narrow, _ = evaluate_title(title, preferences, wide=False)
        wide, family = evaluate_title(title, preferences, wide=True)
        self.narrow[narrow] += 1
        self.wide[wide] += 1
        if score is not None:
            self.scored += 1
        if wide != "drop" or narrow == "drop":
            # Nothing new is lost. (The narrow map drops nothing in the live
            # corpus today, but the arithmetic must not assume that.)
            return
        self.by_family[family or "?"] += 1
        self.by_title[title.lower()] += 1
        if score is None:
            return
        if score > DISCARD_AT_OR_BELOW:
            self.false_drops.append((score, family or "?", title))
        else:
            self.true_drops += 1

    def record_tombstone(self, title: str, preferences: JobPreferences | None) -> None:
        self.tombstones += 1
        outcome, _ = evaluate_title(title, preferences, wide=True)
        if outcome == "drop":
            self.tombstones_dropped += 1

    def merge(self, other: TitleReplay) -> None:
        self.n += other.n
        self.narrow.update(other.narrow)
        self.wide.update(other.wide)
        self.by_family.update(other.by_family)
        self.by_title.update(other.by_title)
        self.scored += other.scored
        self.false_drops.extend(other.false_drops)
        self.true_drops += other.true_drops
        self.tombstones += other.tombstones
        self.tombstones_dropped += other.tombstones_dropped


def load_preferences(doc: dict) -> JobPreferences | None:
    """The user's preferences, or None when absent/unreadable.

    Mirrors ``title_filter.load_job_preferences``: None means "this user's
    discovery does not pre-filter at all", which the report must say out loud
    rather than quietly counting zeros.
    """
    prefs = doc.get("preferences")
    if not prefs:
        return None
    try:
        return JobPreferences.model_validate(prefs)
    except ValidationError:
        return None


async def replay_user(
    db: firestore.AsyncClient, user_id: str, *, with_discarded: bool
) -> TitleReplay | None:
    """Run both maps over one user's persisted jobs. ``None`` = no such user."""
    snap = await db.collection("users").document(user_id).get()
    if not snap.exists:
        print(f"  ! users/{user_id}: no such user — skipped")
        return None
    preferences = load_preferences(snap.to_dict() or {})
    result = TitleReplay(
        user_id=user_id,
        families=list(preferences.target_role_families) if preferences else [],
        no_preferences=preferences is None or not preferences.target_role_families,
    )

    user_ref = db.collection("users").document(user_id)
    async for job_snap in user_ref.collection("jobs").stream():
        doc = job_snap.to_dict() or {}
        title = doc.get("title")
        if not title:
            continue
        match = doc.get("match") or {}
        raw = match.get("overall_score")
        score = float(raw) if isinstance(raw, int | float) else None
        result.record(str(title), preferences, score)

    if with_discarded:
        async for tomb in user_ref.collection("discarded_jobs").stream():
            title = (tomb.to_dict() or {}).get("title")
            if title:
                result.record_tombstone(str(title), preferences)
    return result


def report(r: TitleReplay, *, title: str, top: int) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))
    print(
        f"   target families: {', '.join(r.families) or '(none)'}"
        f"   title map v{TITLE_FILTER_VERSION}"
    )
    print(f"   {r.n} job doc(s), {r.scored} carrying a match")
    if r.no_preferences:
        print(
            "   ! no target_role_families — discovery does not pre-filter this\n"
            "     user at all, so the wide map cannot drop anything here."
        )
    if not r.n:
        return

    print(f"\n   corpus B — every job doc ({r.n})")
    print(f"     {'outcome':<16}{'today':>10}{'wide':>10}{'delta':>10}")
    for outcome in OUTCOMES:
        today, cand = r.narrow[outcome], r.wide[outcome]
        if not today and not cand:
            continue
        print(f"     {outcome:<16}{today:>10}{cand:>10}{cand - today:>+10}")
    dropped = sum(r.by_family.values())
    print(f"\n     newly dropped: {dropped}/{r.n} = {dropped / r.n:.1%} of the corpus")
    if r.narrow["unclassified"]:
        print(
            f"     of the {r.narrow['unclassified']} today-unclassified, "
            f"{r.narrow['unclassified'] - r.wide['unclassified']} "
            f"({(r.narrow['unclassified'] - r.wide['unclassified']) / r.narrow['unclassified']:.1%}) "
            "now classify"
        )

    if r.by_family:
        print("\n   would drop, by family:")
        for family, count in r.by_family.most_common():
            print(f"     {count:6d}  {family}")
        print(f"\n   top {top} titles dropped (read these — they are the map):")
        for job_title, count in r.by_title.most_common(top):
            print(f"     {count:6d}  {job_title[:70]}")

    print(f"\n   corpus A — the {r.scored} match-carrying doc(s)")
    if not r.scored:
        print(
            "     none: this user's backlog was never scored, so this corpus\n"
            "     can neither confirm nor falsify the map here."
        )
    else:
        print(
            f"     false drops : {len(r.false_drops):5d}  "
            f"wide drops it, Pro scored it above {DISCARD_AT_OR_BELOW}"
        )
        print(
            f"     true drops  : {r.true_drops:5d}  "
            f"wide drops it, Pro scored it at or below {DISCARD_AT_OR_BELOW}"
        )
        print(
            "     Survivorship: everything Pro rejected was tombstoned out of\n"
            "     `jobs`, so 0 true drops is expected here and the false-drop\n"
            "     count is the only number this corpus measures."
        )

    if r.tombstones:
        print(
            f"\n   discarded_jobs: {r.tombstones} tombstone(s), "
            f"{r.tombstones_dropped} ({r.tombstones_dropped / r.tombstones:.1%}) "
            "the wide map\n   would have dropped for free — a Flash parse and a "
            "Pro call each, spent on\n   a job Pro went on to reject anyway."
        )

    if r.false_drops:
        print(f"\n   ── every false drop ({len(r.false_drops)}) ──")
        for score, family, job_title in sorted(r.false_drops, reverse=True):
            print(f"     {score:>5}  {family:<16}  {job_title[:60]}")
        print("\n   ^ THE SHIP BAR IS ZERO. Do not flip TITLE_FILTER_WIDE.")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure the widened title map. Read-only, free, no writes."
    )
    parser.add_argument(
        "--user-id",
        action="append",
        dest="user_ids",
        default=[],
        help="User to replay; repeatable. Default: every user in Firestore.",
    )
    parser.add_argument(
        "--with-discarded",
        action="store_true",
        help="Also replay discarded_jobs tombstones (slow; large collection)",
    )
    parser.add_argument(
        "--top", type=int, default=25, help="How many dropped titles to list"
    )
    args = parser.parse_args()

    bind_run_context("title_replay", user_id=",".join(args.user_ids) or "all")
    db = firestore.AsyncClient()
    user_ids = list(args.user_ids)
    if not user_ids:
        async for snap in db.collection("users").stream():
            user_ids.append(snap.id)

    combined = TitleReplay(user_id="ALL")
    reports = 0
    for user_id in user_ids:
        result = await replay_user(db, user_id, with_discarded=args.with_discarded)
        if result is None:
            continue
        report(result, title=f"users/{user_id}", top=args.top)
        combined.merge(result)
        reports += 1

    if reports > 1:
        combined.families = ["(mixed)"]
        report(combined, title=f"ALL {reports} users", top=args.top)

    print(
        f"\nTITLE_FILTER_WIDE is not set by this tool and nothing above was "
        f"written.\nFalse drops across every user: {len(combined.false_drops)} "
        "(ship bar: 0)."
    )
    log.info(
        "title_replay.done",
        users=reports,
        n=combined.n,
        scored=combined.scored,
        newly_dropped=sum(combined.by_family.values()),
        false_drops=len(combined.false_drops),
        title_filter_version=TITLE_FILTER_VERSION,
    )


if __name__ == "__main__":
    asyncio.run(main())
