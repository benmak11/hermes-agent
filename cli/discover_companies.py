# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Search for new company boards and append the ones that check out to
unvetted.yaml. Each new slug is probed first (public board GETs, no spend);
only a board that answers with at least one job is added, with its name.

Usage:
    python -m cli.discover_companies [--backend direct|serper]
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv

from obs.logging import bind_run_context
from tools.discovery.dork import (
    DirectGoogleBackend,
    SerperBackend,
    SweepResult,
    run_sweep,
)

load_dotenv()


def print_summary(result: SweepResult) -> None:
    """What was added, then what was rejected and why, per platform."""
    total = sum(result.added.values())
    if total == 0:
        print("✓ No new companies discovered this run.")
    else:
        print(f"✓ Added {total} new companies to unvetted.yaml:")
        for platform, count in result.added.items():
            if count:
                print(f"  - {platform}: {count}")

    rejected = {
        platform: {r: c for r, c in reasons.items() if c}
        for platform, reasons in result.rejected.items()
    }
    total_rejected = sum(sum(r.values()) for r in rejected.values())
    if total_rejected:
        print(f"✗ Rejected {total_rejected} new slugs whose board did not check out:")
        for platform, reasons in rejected.items():
            if reasons:
                detail = ", ".join(f"{r} {c}" for r, c in reasons.items())
                print(f"  - {platform}: {detail}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        choices=["serper", "direct"],
        default=os.environ.get("DISCOVERY_BACKEND", "direct"),
    )
    args = parser.parse_args()
    bind_run_context("company_sweep", backend=args.backend)

    if args.backend == "serper":
        api_key = os.environ["SERPER_API_KEY"]
        backend = SerperBackend(api_key)
    else:
        backend = DirectGoogleBackend()

    print(f"→ Running company sweep via {args.backend} backend...")
    result = await run_sweep(backend)
    print_summary(result)
    print()
    print("Review data/companies/unvetted.yaml when you next open the vetting UI.")


if __name__ == "__main__":
    asyncio.run(main())
