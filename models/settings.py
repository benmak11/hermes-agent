# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""User-tunable agent settings, stored on ``users/{uid}`` outside the profile."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# Offered cadences, in hours — kept to a few choices so the UI stays a chip row.
IntervalHours = Literal[6, 12, 24, 72]


class DiscoverySettings(BaseModel):
    """How often the agents work unattended. Both loops are opt-in.

    ``auto_discovery`` runs the discovery pipeline and scores the new arrivals;
    ``liveness_sweep`` re-checks already-discovered postings against their ATS
    and dismisses the ones that were taken down, so the review queue, shelves,
    and application tracking never serve a dead posting.
    """

    auto_discovery: bool = False
    discovery_interval_hours: IntervalHours = 24
    liveness_sweep: bool = False
    sweep_interval_hours: IntervalHours = 24
