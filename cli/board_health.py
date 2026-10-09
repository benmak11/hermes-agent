# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Report what board health found, and apply the boards that moved to the YAML.

Reads only the shared ``board_health`` collection (no user data) through a
client pinned to ``GOOGLE_CLOUD_PROJECT``. No probes, no spend.

By default it prints ``moved`` boards (original → target, with both names),
``quarantined`` boards (the candidates), ``dead`` boards (the 404 span, with a
suggested blocklist entry) and ``failing`` boards, and writes nothing.
``--write`` applies only the ``moved`` boards: each entry moves to its new
platform in known.yaml / unvetted.yaml, keeping its name and comments, with
the target's slug. A move whose target is already listed or blocklisted is
skipped and printed. blocklist.yaml is never written: dead boards are
suggestions for a human.

Usage:
    python -m cli.board_health
    python -m cli.board_health --write
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from dotenv import load_dotenv

from cli.eval_scoring import firestore_client
from obs.logging import bind_run_context
from tools import companies as tc
from tools.ats import board_health as bh

# Pins GOOGLE_CLOUD_PROJECT from the project-root .env: ADC's default quota
# project on a dev machine is a different GCP project.
load_dotenv()

FILES = ("known.yaml", "unvetted.yaml")


async def read_records(db) -> dict[str, dict]:
    return {
        snap.id: snap.to_dict() or {}
        async for snap in db.collection(bh.COLLECTION).stream()
    }


@dataclass(frozen=True)
class Move:
    """A moved board and where it now lives, with the name on each side."""

    platform: str
    slug: str
    to_platform: str
    to_slug: str
    name: str | None
    target_name: str | None


@dataclass
class Report:
    moved: list[Move] = field(default_factory=list)
    quarantined: list[dict] = field(default_factory=list)
    dead: list[dict] = field(default_factory=list)
    failing: list[dict] = field(default_factory=list)
    skipped: list[tuple[Move, str]] = field(default_factory=list)
    written: dict[str, int] = field(default_factory=dict)


def _order(rec: dict) -> tuple:
    return (-(rec.get("not_found_days") or 0), rec.get("platform"), rec.get("slug"))


def build_report(records: dict[str, dict], names: dict[tuple[str, str], str]) -> Report:
    report = Report()
    for rec in sorted(records.values(), key=_order):
        state = rec.get("state")
        if state == bh.STATE_MOVED:
            target = bh.target_of(rec)
            if target is None:
                continue
            tp, ts = target
            target_name = next(
                (
                    c.get("name")
                    for c in rec.get("candidates") or []
                    if (c.get("platform"), c.get("slug")) == target
                ),
                None,
            )
            report.moved.append(
                Move(
                    rec["platform"],
                    rec["slug"],
                    tp,
                    ts,
                    names.get((rec["platform"], rec["slug"].casefold())),
                    target_name,
                )
            )
        elif state == bh.STATE_QUARANTINED:
            report.quarantined.append(rec)
        elif state == bh.STATE_DEAD:
            report.dead.append(rec)
        elif state == bh.STATE_FAILING:
            report.failing.append(rec)
    return report


def _span(rec: dict) -> str:
    first, last = rec.get("first_not_found_day"), rec.get("last_not_found_day")
    days = rec.get("not_found_days") or 0
    if first and last:
        return f"404 on {days} days, {first} to {last}"
    return f"404 on {days} days"


def blocklist_suggestion(rec: dict, today: date) -> str:
    """A ``blocklist.yaml`` entry for a dead board, for a human to paste."""
    reason = f"{_span(rec)}; no board on another platform"
    return (
        f"  - platform: {rec['platform']}\n"
        f"    slug: {tc._yaml_scalar(rec['slug'])}\n"
        f'    blocked_at: "{today.isoformat()}"\n'
        f'    reason: "{reason}"'
    )


def write_moves(report: Report) -> dict[str, int]:
    """Apply each move to both YAML files. Targets already listed or
    blocklisted are skipped into ``report.skipped``."""
    listed = {
        (plat, e.slug.casefold())
        for groups in (tc.load_known(), tc.load_unvetted())
        for plat, entries in groups.items()
        for e in entries
    }
    blocked = {(p, s.casefold()) for p, s in tc.load_blocklist()}
    moves: dict[tuple[str, str], tuple[str, str]] = {}
    for move in report.moved:
        target = (move.to_platform, move.to_slug.casefold())
        if target in blocked:
            report.skipped.append((move, "target is blocklisted"))
        elif target in listed:
            report.skipped.append((move, "target is already listed"))
        else:
            moves[(move.platform, move.slug)] = (move.to_platform, move.to_slug)
    return {f: tc.move_entries(f, moves) if moves else 0 for f in FILES}


def _board(rec: dict) -> str:
    return f"{rec.get('platform')}/{rec.get('slug')}"


def print_report(report: Report, *, write: bool, today: date) -> None:
    print(f"\nMoved ({len(report.moved)}):")
    for m in report.moved:
        print(
            f"  → {m.platform}/{m.slug} → {m.to_platform}/{m.to_slug}"
            f"  ({m.name!r} = {m.target_name!r})"
        )
    print(f"\nQuarantined — needs a human ({len(report.quarantined)}):")
    for rec in report.quarantined:
        print(f"  ? {_board(rec)}  ({_span(rec)})")
        for c in rec.get("candidates") or []:
            print(
                f"      {c.get('platform')}/{c.get('slug')}: {c.get('name')!r},"
                f" {c.get('job_count')} jobs"
            )
    print(f"\nDead — skipped by discovery ({len(report.dead)}):")
    for rec in report.dead:
        print(
            f"  ✗ {_board(rec)}  ({_span(rec)}, next check {rec.get('next_check_at')})"
        )
    if report.dead:
        print("\nSuggested blocklist.yaml entries (not written):")
        for rec in report.dead:
            print(blocklist_suggestion(rec, today))
    print(f"\nFailing ({len(report.failing)}):")
    for rec in report.failing:
        status = rec.get("last_status")
        print(
            f"  ! {_board(rec)}: {rec.get('last_outcome')}"
            f"{f' {status}' if status is not None else ''},"
            f" {rec.get('not_found_days') or 0} 404 days,"
            f" since {rec.get('failing_since')}"
        )
    for move, why in report.skipped:
        print(
            f"\nNot moved: {move.platform}/{move.slug} → {move.to_platform}/{move.to_slug}: {why}"
        )

    print(
        f"\n{len(report.moved)} moved, {len(report.quarantined)} quarantined, "
        f"{len(report.dead)} dead, {len(report.failing)} failing."
    )
    if not write:
        print("Report only — nothing written. Re-run with --write to apply the moves.")
    else:
        for filename, n in report.written.items():
            print(f"Moved {n} entries in {filename}.")


async def run(*, write: bool = False, db=None, today: date | None = None) -> Report:
    db = db or firestore_client()
    records = await read_records(db)
    report = build_report(records, tc.entry_names())
    if write:
        report.written = write_moves(report)
    print_report(report, write=write, today=today or datetime.now(UTC).date())
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Report board health and apply moved boards to the YAML."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="move the 'moved' entries in known.yaml / unvetted.yaml",
    )
    args = parser.parse_args(argv)
    bind_run_context("board_health", write=args.write)
    asyncio.run(run(write=args.write))


if __name__ == "__main__":
    main()
