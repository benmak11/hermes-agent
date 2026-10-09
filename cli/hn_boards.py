# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Mine Hacker News "Who is hiring?" threads for new company boards.

Reads the newest thread(s) through the public HN Algolia API, pulls the
greenhouse / lever / ashby / workable board slugs linked from top-level posts,
and probes each slug new to the pool (public board GETs, no spend). Dry run by
default: prints what was found, what is already in the pool, and which boards
would be added (with name) or rejected (with reason). ``--write`` appends the
accepted boards to unvetted.yaml; nothing else is ever written.

Usage:
    python -m cli.hn_boards [--months N]
    python -m cli.hn_boards --write
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from obs.logging import bind_run_context
from tools import companies as tc
from tools.discovery import dork
from tools.discovery.hn import HN_PLATFORMS, MAX_MONTHS, Harvest, harvest


@dataclass
class PlatformReport:
    """One platform's slugs: found (with post counts), in pool, probe results."""

    found: dict[str, int] = field(default_factory=dict)
    in_pool: list[str] = field(default_factory=list)
    accepted: list[tuple[str, str | None]] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    written: int = 0


async def build_reports(found: Harvest) -> dict[str, PlatformReport]:
    """Split each platform's slugs into already-in-pool and new, and vet the new."""
    reports: dict[str, PlatformReport] = {}
    for platform in HN_PLATFORMS:
        slugs = found.slugs[platform]
        new = tc.new_unvetted_slugs(platform, list(slugs))
        new_set = set(new)
        accepted, rejected = await dork.vet_slugs_detailed(platform, new)
        reports[platform] = PlatformReport(
            found=dict(slugs),
            in_pool=[s for s in slugs if s not in new_set],
            accepted=accepted,
            rejected=rejected,
        )
    return reports


def print_report(
    found: Harvest, reports: dict[str, PlatformReport], *, write: bool
) -> None:
    for thread in found.threads:
        print(f"  thread {thread.id}: {thread.title}")
    print(f"  {found.posts} top-level posts scanned.")
    for error in found.errors:
        print(f"  ! API failure, results are partial: {error}")

    for platform, report in reports.items():
        print(f"\n{platform}: {len(report.found)} slugs found")
        if report.in_pool:
            print(
                f"  already in pool ({len(report.in_pool)}): {', '.join(report.in_pool)}"
            )
        for slug, name in report.accepted:
            posts = report.found.get(slug, 0)
            print(f"  + {slug}: {name or '(no name)'} [{posts} posts]")
        for slug, reason in report.rejected:
            print(f"  ✗ {slug}: {reason}")

    added = sum(len(r.accepted) for r in reports.values())
    rejected = sum(len(r.rejected) for r in reports.values())
    print(f"\n{added} boards to add, {rejected} rejected.")
    if not write:
        print("Dry run — nothing written. Re-run with --write to append these.")
    else:
        written = sum(r.written for r in reports.values())
        print(f"Wrote {written} boards to unvetted.yaml.")


async def run(*, months: int = 1, write: bool = False) -> dict[str, PlatformReport]:
    print(f"→ Reading the latest {months} 'Who is hiring?' thread(s)...")
    found = await harvest(months)
    reports = await build_reports(found)
    if write:
        for platform, report in reports.items():
            report.written = tc.append_unvetted(platform, report.accepted)
    print_report(found, reports, write=write)
    return reports


def _months(value: str) -> int:
    n = int(value)
    if not 1 <= n <= MAX_MONTHS:
        raise argparse.ArgumentTypeError(f"must be 1..{MAX_MONTHS}")
    return n


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Harvest board links from HN 'Who is hiring?' threads."
    )
    parser.add_argument(
        "--months",
        type=_months,
        default=1,
        help=f"how many monthly threads to read, newest first (1-{MAX_MONTHS})",
    )
    parser.add_argument(
        "--write", action="store_true", help="append accepted boards to unvetted.yaml"
    )
    args = parser.parse_args(argv)
    bind_run_context("hn_boards", write=args.write, months=args.months)
    asyncio.run(run(months=args.months, write=args.write))


if __name__ == "__main__":
    main()
