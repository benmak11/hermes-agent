# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Journey model: one hiring process past "applied", as a left-to-right track.

Whole-document and client-owned: the web app builds the document, the route
validates shape plus two invariants and forces the server-owned fields. The
optional sub-shapes (``checkin``, ``questions``, ``prep``, ``retro``) are all
default-valued so later screens are additive field use, not migrations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

JourneySource = Literal["hermes", "manual"]
JourneyOutcome = Literal["in_progress", "offer", "rejected", "withdrawn"]
StageStatus = Literal["done", "current", "upcoming"]


class CheckIn(BaseModel):
    # 1 Rough … 5 Great; None = a legacy note imported without a rating.
    rating: int | None = Field(default=None, ge=1, le=5)
    tags: list[str] = Field(default_factory=list)
    sentence: str | None = None
    at: datetime


class PrepItem(BaseModel):
    text: str
    done: bool = False
    # Set when the item was carried over from another journey's retro.
    from_journey_id: str | None = None


class StageQuestion(BaseModel):
    text: str
    topic: str | None = None
    felt: Literal["good", "struggled"] | None = None


class JourneyStage(BaseModel):
    id: str
    name: str
    status: StageStatus
    scheduled_at: datetime | None = None
    format: str | None = None
    who: str | None = None
    checkin: CheckIn | None = None
    questions: list[StageQuestion] = Field(default_factory=list)
    prep: list[PrepItem] = Field(default_factory=list)


class Retro(BaseModel):
    keep: str | None = None
    change: str | None = None
    next: str | None = None


class Journey(BaseModel):
    # Server-owned: whatever the client sends here is overwritten by the route.
    id: str = ""
    user_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Client-owned.
    company: str
    role: str
    source: JourneySource
    application_id: str | None = None
    job_url: str | None = None
    stages: list[JourneyStage] = Field(default_factory=list)
    outcome: JourneyOutcome = "in_progress"
    ended_at_stage_id: str | None = None
    retro: Retro | None = None

    @model_validator(mode="after")
    def _invariants(self) -> Journey:
        ids = [s.id for s in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("stage ids must be unique within a journey")
        if sum(s.status == "current" for s in self.stages) > 1:
            raise ValueError("at most one stage may be current")
        return self
