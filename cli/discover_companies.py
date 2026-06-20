# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""
Usage:
    python -m cli.discover_companies [--backend direct|serper]
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv

from tools.discovery.dork import DirectGoogleBackend, SerperBackend, run_sweep

load_dotenv()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend",
        choices=["serper", "direct"],
        default=os.environ.get("DISCOVERY_BACKEND", "direct"),
    )
    args = parser.parse_args()

    if args.backend == "serper":
        api_key = os.environ["SERPER_API_KEY"]
        backend = SerperBackend(api_key)
    else:
        backend = DirectGoogleBackend()

    print(f"→ Running company sweep via {args.backend} backend...")
    added = await run_sweep(backend)

    total = sum(added.values())
    if total == 0:
        print("✓ No new companies discovered this run.")
    else:
        print(f"✓ Added {total} new companies to unvetted.yaml:")
        for platform, count in added.items():
            if count:
                print(f"  - {platform}: {count}")
    print()
    print("Review data/companies/unvetted.yaml when you next open the vetting UI.")


if __name__ == "__main__":
    asyncio.run(main())
