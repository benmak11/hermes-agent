# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for the liveness-sweep helpers and discovery settings model."""

import pytest
from pydantic import ValidationError

from models.settings import DiscoverySettings
from tools.ats.validate import BOARD_URLS, live_ids


def test_live_ids_greenhouse_and_ashby_wrap_jobs() -> None:
    data = {"jobs": [{"id": 123}, {"id": "abc"}, {"title": "no id"}]}
    assert live_ids("greenhouse", data) == {"123", "abc"}
    assert live_ids("ashby", data) == {"123", "abc"}


def test_live_ids_lever_bare_array() -> None:
    assert live_ids("lever", [{"id": "x1"}, {"id": "x2"}]) == {"x1", "x2"}


def test_live_ids_workable_uses_shortcode() -> None:
    data = {"jobs": [{"shortcode": "38ABFA8E0D"}, {"id": "not-a-shortcode"}]}
    assert live_ids("workable", data) == {"38ABFA8E0D"}


def test_live_ids_recruitee_uses_offers() -> None:
    assert live_ids("recruitee", {"offers": [{"id": 2429738}]}) == {"2429738"}


def test_live_ids_empty_or_missing() -> None:
    assert live_ids("greenhouse", {}) == set()
    assert live_ids("greenhouse", None) == set()
    assert live_ids("lever", None) == set()
    assert live_ids("recruitee", None) == set()


def test_board_urls_cover_board_platforms() -> None:
    assert BOARD_URLS["greenhouse"]("acme").endswith("/acme/jobs")
    assert "acme?mode=json" in BOARD_URLS["lever"]("acme")
    assert BOARD_URLS["ashby"]("acme").endswith("/acme")
    assert BOARD_URLS["workable"]("acme").endswith("/accounts/acme")
    assert BOARD_URLS["recruitee"]("acme") == "https://acme.recruitee.com/api/offers/"
    # SmartRecruiters listing is paginated — membership checks would be wrong,
    # so it must NOT be board-batched (it has a per-posting endpoint instead).
    assert "smartrecruiters" not in BOARD_URLS


def test_discovery_settings_defaults_off() -> None:
    s = DiscoverySettings()
    assert s.auto_discovery is False
    assert s.liveness_sweep is False
    assert s.discovery_interval_hours == 24


def test_discovery_settings_rejects_arbitrary_interval() -> None:
    with pytest.raises(ValidationError):
        DiscoverySettings(discovery_interval_hours=5)
