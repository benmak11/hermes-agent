# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for company-slug extraction from search result URLs."""

from tools.discovery.dork import extract_slugs


def test_extract_slugs_greenhouse_only() -> None:
    urls = [
        "https://boards.greenhouse.io/stripe/jobs/123",
        "https://boards.greenhouse.io/airbnb",
        "https://jobs.lever.co/spotify/abc",  # wrong platform -> ignored
        "https://example.com/whatever",
    ]
    assert extract_slugs(urls, "greenhouse") == {"stripe", "airbnb"}


def test_extract_slugs_per_platform() -> None:
    urls = [
        "https://jobs.lever.co/spotify/abc-123",
        "https://jobs.ashbyhq.com/ramp/xyz",
    ]
    assert extract_slugs(urls, "lever") == {"spotify"}
    assert extract_slugs(urls, "ashby") == {"ramp"}


def test_extract_slugs_filters_platform_internal_paths() -> None:
    urls = [
        "https://boards.greenhouse.io/search",
        "https://boards.greenhouse.io/api",
        "https://boards.greenhouse.io/jobs",
    ]
    assert extract_slugs(urls, "greenhouse") == set()
