# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Company management endpoints: list and promote/block/dismiss/pause."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.deps import verify_user
from obs.logging import get_logger
from tools.companies import (
    apply_company_action,
    load_blocklist_detailed,
    load_known,
    load_unvetted,
)

router = APIRouter(tags=["companies"])
log = get_logger("api.companies")


@router.get("/companies")
def list_companies(user_id: str = Depends(verify_user)) -> dict:
    """Return the known / unvetted / blocklist company sets."""
    return {
        "known": {
            p: [c.model_dump(mode="json") for c in v]
            for p, v in load_known().items()
        },
        "unvetted": {
            p: [c.model_dump(mode="json") for c in v]
            for p, v in load_unvetted().items()
        },
        "blocklist": load_blocklist_detailed(),
    }


class CompanyAction(BaseModel):
    platform: Literal["greenhouse", "lever", "ashby"]
    slug: str
    action: Literal["promote", "block", "dismiss", "pause"]
    reason: str | None = None


@router.post("/companies/action")
def company_action(body: CompanyAction, user_id: str = Depends(verify_user)) -> dict:
    """Apply a promote/block/dismiss/pause action to a company."""
    apply_company_action(body.platform, body.slug, body.action, body.reason)
    log.info(
        "company.action",
        platform=body.platform,
        slug=body.slug,
        action=body.action,
        reason=body.reason,
    )
    return {"ok": True}
