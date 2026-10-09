# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Backfill company names into known.yaml and unvetted.yaml from each board.

Probes every greenhouse / lever / ashby / workable board in the pool (public
GETs, no spend) and reads the company name the board publishes. Dry run by default:
prints the proposed name per board and writes nothing. ``--write`` sets
``name`` only on entries that have none — an existing name is never
overwritten, blocklisted boards are skipped and blocklist.yaml is never
touched. Boards whose probe is not ok are listed for a human, never removed.

Usage:
    python -m cli.board_names [--platform greenhouse|lever|ashby|workable]
                              [--limit N]
    python -m cli.board_names --write
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from obs.logging import bind_run_context
from tools import companies as tc
from tools.ats._http import OK, board_client
from tools.ats.probe import PROBED_PLATFORMS, BoardProbe, probe_board

FILES = {"known": "known.yaml", "unvetted": "unvetted.yaml"}

_CONCURRENCY = 10


@dataclass(frozen=True)
class Board:
    """One pool entry: which file it lives in and the name it already has."""

    source: str
    platform: str
    slug: str
    name: str | None


@dataclass
class Report:
    """What the probes found, and (with ``--write``) how many names were set."""

    proposed: list[tuple[Board, str]] = field(default_factory=list)
    kept: list[tuple[Board, str | None]] = field(default_factory=list)
    nameless: list[Board] = field(default_factory=list)
    not_ok: list[tuple[Board, BoardProbe]] = field(default_factory=list)
    written: dict[str, int] = field(default_factory=dict)


def load_boards(platform: str | None = None, limit: int | None = None) -> list[Board]:
    """Every probe-able board in known then unvetted, minus blocklisted ones."""
    blocked = {(p, s.casefold()) for p, s in tc.load_blocklist()}
    boards: list[Board] = []
    for source, groups in (
        ("known", tc.load_known()),
        ("unvetted", tc.load_unvetted()),
    ):
        for plat, entries in groups.items():
            if plat not in PROBED_PLATFORMS or (platform and plat != platform):
                continue
            boards.extend(
                Board(source, plat, e.slug, e.name)
                for e in entries
                if (plat, e.slug.casefold()) not in blocked
            )
    return boards[:limit] if limit is not None else boards


async def probe_all(boards: list[Board]) -> list[BoardProbe]:
    """Probe ``boards`` at most :data:`_CONCURRENCY` at a time, in order."""
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(board: Board) -> BoardProbe:
        async with sem:
            return await probe_board(board.platform, board.slug)

    async with board_client():
        return await asyncio.gather(*(one(b) for b in boards))


def build_report(boards: list[Board], probes: list[BoardProbe]) -> Report:
    report = Report()
    for board, probe in zip(boards, probes, strict=True):
        if probe.outcome != OK:
            report.not_ok.append((board, probe))
        if board.name is not None:
            report.kept.append((board, probe.name))
        elif probe.name is not None:
            report.proposed.append((board, probe.name))
        elif probe.outcome == OK:
            report.nameless.append(board)
    return report


def write_names(report: Report) -> dict[str, int]:
    """Set each proposed name in its file. Returns names written per file."""
    written: dict[str, int] = {}
    for source, filename in FILES.items():
        names = {
            (b.platform, b.slug): name
            for b, name in report.proposed
            if b.source == source
        }
        written[filename] = tc.fill_missing_names(filename, names) if names else 0
    return written


def _label(board: Board) -> str:
    return f"{board.platform}/{board.slug} ({board.source})"


def print_report(report: Report, *, write: bool) -> None:
    for board, name in report.proposed:
        print(f"  + {_label(board)}: {name}")
    for board, probed in report.kept:
        note = f" (board says {probed!r})" if probed and probed != board.name else ""
        print(f"  = {_label(board)}: {board.name} kept{note}")
    if report.nameless:
        print(f"\nNo name on the board ({len(report.nameless)}):")
        for board in report.nameless:
            print(f"  ? {_label(board)}")
    if report.not_ok:
        print(f"\nNot ok — review by hand, nothing removed ({len(report.not_ok)}):")
        for board, probe in report.not_ok:
            status = f" {probe.status}" if probe.status is not None else ""
            print(f"  ✗ {_label(board)}: {probe.outcome}{status}")

    print(
        f"\n{len(report.proposed)} names proposed, {len(report.kept)} kept, "
        f"{len(report.nameless)} with no name, {len(report.not_ok)} not ok."
    )
    if not write:
        print("Dry run — nothing written. Re-run with --write to set these names.")
    else:
        for filename, n in report.written.items():
            print(f"Wrote {n} names to {filename}.")


async def run(
    *, platform: str | None = None, limit: int | None = None, write: bool = False
) -> Report:
    boards = load_boards(platform, limit)
    print(f"→ Probing {len(boards)} boards...")
    report = build_report(boards, await probe_all(boards))
    if write:
        report.written = write_names(report)
    print_report(report, write=write)
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Backfill company names from each board."
    )
    parser.add_argument("--platform", choices=PROBED_PLATFORMS)
    parser.add_argument("--limit", type=int, help="probe only the first N boards")
    parser.add_argument(
        "--write", action="store_true", help="set missing names in the YAML files"
    )
    args = parser.parse_args(argv)
    bind_run_context("board_names", write=args.write)
    asyncio.run(run(platform=args.platform, limit=args.limit, write=args.write))


if __name__ == "__main__":
    main()
