# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Grade the scorer against hand-written labels: AUC, precision/recall at the
queue's threshold of 60, and parse-field accuracy.

Default mode is free and read-only — it joins ``data/eval/labels.jsonl`` to
scores already in Firestore and makes no model call. ``--rescore`` re-runs the
current prompt and model over the labelled set instead; **that spends real
money**, so it prints a cost quote and will not start without an explicit
confirmation (``--yes`` skips only that prompt). A rescore writes its results
to a file and never back to Firestore, so the stored scores stay available to
compare against.

Usage:
    python -m cli.eval_scoring --user-id me
    python -m cli.eval_scoring --user-id me --labels data/eval/labels.jsonl
    python -m cli.eval_scoring --user-id me --rescore [--yes] [--ignore-budget]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import firestore

from models.job import Job
from models.profile import MasterProfile
from obs.llm_cost import run_cost_snapshot
from obs.logging import bind_run_context, get_logger
from tools.matching import budget, rates
from tools.matching.pipeline import match_job
from tools.matching.score import QUEUE_DEFAULT_MIN_SCORE, unbudgeted_limit
from tools.spend import estimate

# Pins GOOGLE_CLOUD_PROJECT from the project-root .env. ADC's default quota
# project on a dev machine is a different GCP project, so the client below is
# constructed with an explicit project rather than letting it be inferred.
load_dotenv()

log = get_logger("cli.eval_scoring")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LABELS = REPO_ROOT / "data" / "eval" / "labels.jsonl"
RESCORE_DIR = REPO_ROOT / "data" / "eval" / "runs"

#: Parse fields a label may correct — every scalar on ``models.job.ParsedJD``
#: that the matching and geo rules read. A label naming anything else is a
#: typo, and is rejected rather than silently counted as "not checked".
PARSE_FIELDS = (
    "role_family",
    "seniority",
    "remote_policy",
    "us_remote_ok",
    "job_country",
    "job_state",
    "job_city",
    "remote_scope",
)

Z95 = 1.96


# ------------------------------------------------------------------ labels


@dataclass(frozen=True)
class Label:
    """One hand-written judgement: is this job a fit, and what did the parse
    get wrong. ``parse`` holds only the fields the label actually corrects."""

    job_id: str
    fit: bool
    parse: dict


def load_labels(path: Path) -> list[Label]:
    """Read ``labels.jsonl``. Blank lines and ``#`` comments are skipped.

    Raises ``SystemExit`` naming the line number for anything malformed — a
    label set that silently drops rows produces a metric over a corpus nobody
    chose.
    """
    if not path.exists():
        raise SystemExit(f"No label file at {path}. See data/eval/README.md.")
    labels: list[Label] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}:{lineno}: not valid JSON ({e})") from None
        if not isinstance(row, dict):
            raise SystemExit(f"{path}:{lineno}: expected a JSON object")
        job_id = row.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise SystemExit(f"{path}:{lineno}: 'job_id' must be a non-empty string")
        if not isinstance(row.get("fit"), bool):
            raise SystemExit(f"{path}:{lineno}: 'fit' must be true or false")
        if job_id in seen:
            raise SystemExit(f"{path}:{lineno}: duplicate job_id {job_id!r}")
        seen.add(job_id)
        parse = row.get("parse") or {}
        if not isinstance(parse, dict):
            raise SystemExit(f"{path}:{lineno}: 'parse' must be an object")
        unknown = sorted(set(parse) - set(PARSE_FIELDS))
        if unknown:
            raise SystemExit(
                f"{path}:{lineno}: unknown parse field(s) {unknown}; "
                f"expected any of {list(PARSE_FIELDS)}"
            )
        labels.append(Label(job_id=job_id, fit=row["fit"], parse=dict(parse)))
    if not labels:
        raise SystemExit(f"{path} holds no labels.")
    return labels


# ------------------------------------------------------- joining to Firestore

JOBS = "jobs"
TOMBSTONES = "discarded_jobs"
MISSING = "missing"
RESCORED = "rescored"


@dataclass(frozen=True)
class Joined:
    """A label beside whatever Firestore (or a rescore) holds for that job."""

    label: Label
    source: str
    doc: dict | None = None

    @property
    def score(self) -> float | None:
        """``overall_score`` from a job doc or a rescore, ``score`` from a
        tombstone — ``None`` when the job was never scored."""
        if not self.doc:
            return None
        match = self.doc.get("match")
        if isinstance(match, dict) and match.get("overall_score") is not None:
            return float(match["overall_score"])
        if self.doc.get("score") is not None:
            return float(self.doc["score"])
        return None

    @property
    def jd_parsed(self) -> dict | None:
        parsed = (self.doc or {}).get("jd_parsed")
        return parsed if isinstance(parsed, dict) else None

    def job(self) -> Job | None:
        """The ``Job`` a rescore would re-run, or ``None`` when it cannot.

        A tombstone keeps no ``jd_raw`` unless it carries a ``restore``
        payload (only geo-enforced skips do), so most rejected jobs are not
        rescorable without refetching the posting.
        """
        doc = self.doc or {}
        payload = doc if self.source == JOBS else doc.get("restore")
        if not isinstance(payload, dict):
            return None
        try:
            return Job.model_validate(payload)
        except Exception:
            return None


async def join_labels(db, user_id: str, labels: list[Label]) -> list[Joined]:
    """Look each label up in ``jobs``, then in ``discarded_jobs``.

    Both collections, always: everything the scorer rated at or below 20 was
    moved to a tombstone, so a join over ``jobs`` alone drops the negative
    class entirely and computes AUC over positives only.
    """
    user_ref = db.collection("users").document(user_id)
    rows: list[Joined] = []
    for label in labels:
        snap = await user_ref.collection(JOBS).document(label.job_id).get()
        if snap.exists:
            rows.append(Joined(label, JOBS, snap.to_dict() or {}))
            continue
        snap = await user_ref.collection(TOMBSTONES).document(label.job_id).get()
        if snap.exists:
            rows.append(Joined(label, TOMBSTONES, snap.to_dict() or {}))
            continue
        rows.append(Joined(label, MISSING))
    return rows


def split(rows: list[Joined]) -> dict:
    """Where the labelled jobs were found, and how many carry a score."""
    found = Counter(row.source for row in rows)
    return {
        "labels": len(rows),
        JOBS: found[JOBS],
        TOMBSTONES: found[TOMBSTONES],
        MISSING: found[MISSING],
        "scored": sum(1 for row in rows if row.score is not None),
    }


# ----------------------------------------------------------------- metrics


@dataclass(frozen=True)
class Undefined:
    """A metric this label set cannot support, and why. Printed as such —
    never substituted with 0.5, 0, or a crash."""

    reason: str


@dataclass(frozen=True)
class Auc:
    """Tie-corrected AUC with a normal-approximation 95% interval."""

    value: float
    lo: float
    hi: float
    n_fit: int
    n_unfit: int
    tied_pairs: int

    @property
    def spans_half(self) -> bool:
        """True when the interval contains 0.5 — the set cannot distinguish
        the scorer from a coin flip, whatever the point estimate says."""
        return self.lo <= 0.5 <= self.hi


def average_ranks(values: list[float]) -> list[float]:
    """1-based ranks, with tied values sharing their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def auc(fit: list[float], unfit: list[float]) -> Auc | Undefined:
    """AUC of the scores, as the Mann-Whitney U statistic over average ranks.

    Average ranks are what makes this tie-corrected, and ties are the normal
    case here: geographically ineligible jobs are all capped at exactly 20,
    and one past corpus held a single distinct score end to end. Counting
    pairs where a fit job outscored an unfit one instead would score every tie
    as a loss and read far below the truth.

    The interval is the Hanley-McNeil standard error at 95%, clamped to
    [0, 1]. Both classes are required; otherwise :class:`Undefined`.

    At a perfect 0 or 1 that standard error is exactly zero at any sample
    size, which would print a zero-width interval around 1.000 off four
    labels. Those two values get a rule-of-three bound over the pair count
    instead, so the interval still narrows with evidence and never claims
    certainty the set cannot support.
    """
    n_fit, n_unfit = len(fit), len(unfit)
    if not n_fit or not n_unfit:
        return Undefined(f"needs both classes; have {n_fit} fit / {n_unfit} unfit")

    pairs = n_fit * n_unfit
    ranks = average_ranks(fit + unfit)
    u = sum(ranks[:n_fit]) - n_fit * (n_fit + 1) / 2.0
    value = u / pairs

    fit_counts, unfit_counts = Counter(fit), Counter(unfit)
    tied = sum(count * unfit_counts[score] for score, count in fit_counts.items())

    q1 = value / (2.0 - value)
    q2 = 2.0 * value**2 / (1.0 + value)
    variance = (
        value * (1.0 - value)
        + (n_fit - 1) * (q1 - value**2)
        + (n_unfit - 1) * (q2 - value**2)
    ) / pairs
    half = Z95 * math.sqrt(max(variance, 0.0))
    if value in (0.0, 1.0):
        half = min(3.0 / pairs, 1.0)
    return Auc(
        value=value,
        lo=max(value - half, 0.0),
        hi=min(value + half, 1.0),
        n_fit=n_fit,
        n_unfit=n_unfit,
        tied_pairs=tied,
    )


@dataclass(frozen=True)
class Threshold:
    """The 2x2 table at one score cut-off."""

    threshold: float
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float | Undefined:
        if not self.tp + self.fp:
            return Undefined(f"no job scored >= {self.threshold:g}")
        return self.tp / (self.tp + self.fp)

    @property
    def recall(self) -> float | Undefined:
        if not self.tp + self.fn:
            return Undefined("no labelled fit job")
        return self.tp / (self.tp + self.fn)


def at_threshold(rows: list[Joined], threshold: float) -> Threshold:
    """Precision/recall table for "the queue would have shown this job"."""
    tp = fp = fn = tn = 0
    for row in rows:
        if row.score is None:
            continue
        shown = row.score >= threshold
        if row.label.fit:
            tp, fn = (tp + 1, fn) if shown else (tp, fn + 1)
        else:
            fp, tn = (fp + 1, tn) if shown else (fp, tn + 1)
    return Threshold(threshold=threshold, tp=tp, fp=fp, fn=fn, tn=tn)


@dataclass
class FieldTally:
    """One parse field's agreement: ``agreed`` out of ``checked``."""

    checked: int = 0
    agreed: int = 0
    no_parse: int = 0


def _norm(value):
    """Compare case- and whitespace-insensitively; everything else verbatim."""
    return value.strip().casefold() if isinstance(value, str) else value


def parse_field_accuracy(rows: list[Joined]) -> dict[str, FieldTally]:
    """Per-field agreement, keyed by field and never averaged together.

    Only the fields a label actually corrects are counted — a field the label
    omits was not checked, and counting it as agreement turns every unexamined
    field into a free point. One number across the fields would hide which
    field is broken, since their base rates are nothing alike.
    """
    tallies: dict[str, FieldTally] = {}
    for row in rows:
        parsed = row.jd_parsed
        for key, expected in row.label.parse.items():
            tally = tallies.setdefault(key, FieldTally())
            tally.checked += 1
            if parsed is None:
                tally.no_parse += 1
                continue
            if _norm(parsed.get(key)) == _norm(expected):
                tally.agreed += 1
    return tallies


# ------------------------------------------------------------------ report


def _pct(metric: float | Undefined) -> str:
    if isinstance(metric, Undefined):
        return f"undefined ({metric.reason})"
    return f"{metric:.3f}"


def report(rows: list[Joined], *, user_id: str, labels_path: Path, title: str) -> None:
    """Print the whole report. Pure output: reads nothing, writes nothing."""
    counts = split(rows)
    print(f"\n── {title}: users/{user_id} " + "─" * max(0, 40 - len(title)))
    print(f"   {counts['labels']} label(s) from {labels_path}")
    print(
        f"   found {counts[JOBS]} in jobs, {counts[TOMBSTONES]} in discarded_jobs, "
        f"{counts[MISSING]} missing"
    )
    unscored = counts[JOBS] + counts[TOMBSTONES] - counts["scored"]
    print(f"   {counts['scored']} carry a stored score ({unscored} found but unscored)")

    fit = [r.score for r in rows if r.score is not None and r.label.fit]
    unfit = [r.score for r in rows if r.score is not None and not r.label.fit]
    result = auc(fit, unfit)
    print("\n   AUC of overall_score against fit")
    if isinstance(result, Undefined):
        print(f"     undefined ({result.reason})")
    else:
        print(
            f"     {result.value:.3f}   95% CI [{result.lo:.3f}, {result.hi:.3f}]"
            f"   from {result.n_fit} fit / {result.n_unfit} unfit"
            f", {result.tied_pairs} tied pair(s)"
        )
        if result.spans_half:
            print(
                "     The interval spans 0.5, so this label set does not show "
                "the scorer\n     ranking better than chance. Read it as "
                "inconclusive, not as the point\n     estimate."
            )

    table = at_threshold(rows, QUEUE_DEFAULT_MIN_SCORE)
    print(f"\n   at the queue threshold (score >= {QUEUE_DEFAULT_MIN_SCORE})")
    print(f"     tp {table.tp}  fp {table.fp}  fn {table.fn}  tn {table.tn}")
    print(f"     precision {_pct(table.precision)}")
    print(f"     recall    {_pct(table.recall)}")

    tallies = parse_field_accuracy(rows)
    print("\n   parse fields (agreed / checked, never averaged)")
    if not tallies:
        print("     no label corrects a parse field")
    for name in PARSE_FIELDS:
        tally = tallies.get(name)
        if tally is None:
            continue
        note = f"   ({tally.no_parse} with no stored parse)" if tally.no_parse else ""
        print(f"     {name:<14} {tally.agreed:>3} / {tally.checked}{note}")


# ----------------------------------------------------------------- rescore


def rescore_quote(n_jobs: int, *, limits: budget.Limits | None = None):
    """Cost quote for rescoring ``n_jobs``, at the **rated-job** rate.

    Every job in an eval set pays both legs — a Flash parse and a Pro score —
    so the blended per-attempted rate (:data:`rates.MEASURED_COST_PER_JOB_USD`,
    which averages in jobs rejected for free) understates this by about half.
    Units are the eval set, not a budget grant, so the caller prints the
    budget separately.

    The floor is raised back to the full rate afterwards: ``quote`` discounts
    it to the batch price above 50 units, and a rescore has no batch path —
    it calls ``match_job`` per job, online.
    """
    quote = estimate.quote(
        estimate.SCORE_BACKLOG,
        remaining_cycle=n_jobs,
        remaining_day=n_jobs,
        rate=rates.MEASURED_RATED,
        limits=limits,
    )
    return replace(quote, usd_low=round(quote.units * quote.rate_usd, 2))


def confirm(question: str, *, assume_yes: bool) -> bool:
    """Ask before spending. ``--yes`` is the only thing that skips the prompt,
    and anything but a typed ``yes`` is a no (including a closed stdin)."""
    if assume_yes:
        print(f"{question} yes (--yes)")
        return True
    try:
        return input(f"{question} ").strip().lower() == "yes"
    except EOFError:
        return False


def _result_line(row: Joined) -> str:
    return json.dumps(
        {
            "job_id": row.label.job_id,
            "fit": row.label.fit,
            "score": row.score,
            "jd_parsed": row.jd_parsed,
        }
    )


async def rescore(
    db,
    user_id: str,
    rows: list[Joined],
    *,
    assume_yes: bool,
    ignore_budget: bool,
    out_path: Path,
) -> list[Joined] | None:
    """Re-run the current prompt and model over the labelled set.

    **Spends real money**, and returns ``None`` without making a single model
    call unless the quote is confirmed. Results are appended to ``out_path`` as
    each job finishes, before anything else can fail — a paid run that loses
    its output has to be paid for twice. Nothing is written back to Firestore,
    so the stored scores remain available to compare against.
    """
    rescorable = [row for row in rows if row.job() is not None]
    skipped = len(rows) - len(rescorable)
    if not rescorable:
        print(
            "\n   Nothing to rescore: no labelled job carries a job description. "
            "A tombstone keeps no jd_raw unless it was a geo-enforced skip."
        )
        return None

    snap = await db.collection("users").document(user_id).get()
    if not snap.exists:
        raise SystemExit(f"No profile at users/{user_id}. Run `cli.sync_profile`.")
    user_doc = snap.to_dict() or {}
    profile = MasterProfile.model_validate(user_doc)

    limits = budget.Limits.from_env()
    quote = rescore_quote(len(rescorable), limits=limits)
    remaining_cycle, remaining_day = estimate.available(
        user_doc.get(budget.FIELD), action=estimate.SCORE_BACKLOG, limits=limits
    )
    grant = min(remaining_cycle, remaining_day)

    print("\n── rescore quote " + "─" * 44)
    print(f"   {quote.units} job(s) to score ({skipped} labelled job(s) cannot be)")
    print(
        f"   ${quote.usd_low:.2f} - ${quote.usd_high:.2f} at "
        f"${quote.rate_usd:.6f}/rated job ({quote.rate_source})"
    )
    if ignore_budget:
        print(
            f"   --ignore-budget: the per-user scoring budget ({grant} slot(s) "
            "left) will not be charged or respected."
        )
    elif grant < quote.units:
        print(
            f"   The per-user scoring budget grants {grant} of these "
            f"{quote.units} job(s); the rest will NOT be scored. Raise "
            "SCORING_BUDGET_PER_CYCLE / _PER_DAY, or pass --ignore-budget."
        )

    if not confirm("   Spend this? Type 'yes' to proceed:", assume_yes=assume_yes):
        print("   Aborted. No model call was made.")
        return None

    reservation = None
    if ignore_budget:
        log.warning("eval_scoring.budget_ignored", user_id=user_id)
        rescorable = rescorable[: unbudgeted_limit(None)]
    else:
        reservation = await budget.reserve(db, user_id, len(rescorable), cycle_id=None)
        if not reservation.granted:
            print("   The scoring budget is exhausted; nothing was scored.")
            return None
        rescorable = rescorable[: reservation.granted]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fresh: list[Joined] = []
    attempted = 0
    try:
        with out_path.open("w") as out:
            for row in rescorable:
                job = row.job()
                attempted += 1
                try:
                    match = await match_job(job, profile)
                except Exception as e:
                    print(f"   ✗ {job.id}: {str(e)[:120]}")
                    log.exception("eval_scoring.rescore_failed", job_id=job.id)
                    continue
                scored = Joined(
                    row.label,
                    RESCORED,
                    {
                        "match": match.model_dump(mode="json"),
                        "jd_parsed": (
                            job.jd_parsed.model_dump(mode="json")
                            if job.jd_parsed
                            else None
                        ),
                    },
                )
                # Flushed per job: this line is the only record of a Pro call
                # that has already been paid for.
                out.write(_result_line(scored) + "\n")
                out.flush()
                fresh.append(scored)
    finally:
        if reservation is not None:
            await budget.release(
                db,
                user_id,
                reservation.granted - attempted,
                cycle_id=reservation.cycle_id,
            )
    print(f"\n   Wrote {len(fresh)} rescored job(s) to {out_path}")
    return fresh


# -------------------------------------------------------------------- main


def firestore_client() -> firestore.AsyncClient:
    """An async client pinned to ``GOOGLE_CLOUD_PROJECT``, never to ADC's
    default quota project, which on a dev machine is a different project."""
    project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    assert project, "GOOGLE_CLOUD_PROJECT is unset; cannot pin the Firestore project."
    return firestore.AsyncClient(project=project)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS,
        help="JSONL label file (default: data/eval/labels.jsonl)",
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Re-run the current prompt and model over the set. Costs money",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the --rescore confirmation prompt"
    )
    parser.add_argument(
        "--ignore-budget",
        action="store_true",
        help="--rescore only: skip the per-user scoring budget (real money)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="--rescore only: where to write results (default: data/eval/runs/)",
    )
    args = parser.parse_args()
    if not args.rescore and (args.yes or args.ignore_budget or args.out):
        parser.error("--yes / --ignore-budget / --out apply only to --rescore")

    run_id = bind_run_context("eval_scoring", user_id=args.user_id)
    labels = load_labels(args.labels)
    db = firestore_client()
    rows = await join_labels(db, args.user_id, labels)
    report(rows, user_id=args.user_id, labels_path=args.labels, title="stored scores")

    if not args.rescore:
        return

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = args.out or RESCORE_DIR / f"rescore-{stamp}.jsonl"
    fresh = await rescore(
        db,
        args.user_id,
        rows,
        assume_yes=args.yes,
        ignore_budget=args.ignore_budget,
        out_path=out_path,
    )
    if not fresh:
        return
    report(fresh, user_id=args.user_id, labels_path=out_path, title="rescored")
    spent = run_cost_snapshot(run_id)
    print(f"\n   This run spent ${spent.get('cost_usd', 0.0):.4f}")


if __name__ == "__main__":
    asyncio.run(main())
