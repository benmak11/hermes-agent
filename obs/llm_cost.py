# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Per-call LLM token usage + USD cost telemetry.

Every real ``generate_content`` call site (``tools/matching/pipeline.py``,
``tools/profile/extract.py``, ``tools/tailoring/objective.py``) calls
:func:`record_llm_call` right after the response comes back. It logs one
structured ``llm.call`` event with token counts and a computed cost.

No ``run_id``/``request_id`` plumbing is needed here: ``obs.logging`` already
binds one of those onto structlog's contextvars for every code path that
reaches these call sites (``run_context`` for the CLI/cron/background-task
pipelines, the request middleware for synchronous API routes), and structlog
merges bound contextvars into every log line automatically. Aggregating cost
per application run downstream is therefore just "GROUP BY run_id" over these
log lines — see ``deployment/terraform/shared/llm_cost.sql``.

That sink only exists on Cloud Run, though: a local process logs to stdout
and its ``llm.call`` lines are gone the moment it exits. So the same numbers
are *also* accumulated in process, keyed by ``run_id``, and flushed to a
Firestore run ledger by ``tools.run_costs`` when the run ends — one answer to
"what did that run cost?" that reads the same from a laptop, the API, and the
worker.

Pricing is looked up by ``response.model_version``, and the table is keyed by
whatever string actually shows up there — Vertex echoes the *requested* id
back verbatim and does not resolve an alias to a concrete model (confirmed
live 2026-07-08; an earlier version of this module assumed the opposite).
Both call sites now request concrete pinned ids
(``gemini-3.1-pro-preview``, ``gemini-2.5-flash``), so both lookups hit a
pinned key. The ``gemini-flash-latest`` entry below is kept anyway, because
months of already-written ledger rows and ``llm_call`` log lines carry that
string and a cost replay over them has to price it; it is dead for new calls,
not for history. A model string missing from ``_PRICING_PER_MILLION`` still
gets its token counts logged, just with ``cost_usd=None`` and a warning,
rather than silently reporting a wrong number.
"""

from __future__ import annotations

import copy
from typing import Any

from google.genai.types import GenerateContentResponse

from obs.logging import current_run_id, get_logger

log = get_logger("llm.cost")

# USD per 1,000,000 tokens, standard (<=200K prompt tokens) tier, global
# endpoint. Source: Vertex AI Generative AI pricing page, checked 2026-07.
# Google revises these — re-verify before trusting this for budgeting, and
# before relying on it for paywall pricing.
_PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00, "cached": 0.20},
    # Historical only: no call site requests the alias any more, since both
    # Flash declarations are pinned to the concrete id below. Kept because
    # ledger rows and llm_call log lines written before the pin carry this
    # string and a cost replay over them still has to price it. Same rates —
    # the alias served this model — so dropping the entry would silently turn
    # every pre-pin row into cost_usd=None.
    "gemini-flash-latest": {"input": 0.30, "output": 2.50, "cached": 0.03},
    # Every Flash call site now: the online scorer and tailoring, via
    # tools/llm_models.FLASH_MODEL and tools/tailoring/objective's
    # OBJECTIVE_MODEL, plus the batch scorer via BATCH_FLASH_MODEL (which
    # always had to pin a concrete id, because batch prediction rejects
    # aliases). 2.5 Flash has no long-context pricing tier — flat rate at any
    # input size, unlike Pro — so it needs no entry in the table below.
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cached": 0.03},
}
# Long-context (>200K prompt tokens) rates for the same models, keyed by
# "<model>:long". None of our current prompts get near 200K, but the lookup
# is context-aware so this doesn't silently under-price if that changes.
_LONG_CONTEXT_PRICING_PER_MILLION: dict[str, dict[str, float]] = {
    "gemini-3.1-pro-preview": {"input": 4.00, "output": 18.00, "cached": 0.40},
}
_LONG_CONTEXT_THRESHOLD = 200_000

# Vertex batch prediction bills at half the interactive rate on every token
# class, both models. Source: same pricing page as above, checked 2026-07.
_BATCH_DISCOUNT = 0.5


def _rates(model: str, prompt_tokens: int) -> dict[str, float] | None:
    if (
        prompt_tokens > _LONG_CONTEXT_THRESHOLD
        and model in _LONG_CONTEXT_PRICING_PER_MILLION
    ):
        return _LONG_CONTEXT_PRICING_PER_MILLION[model]
    return _PRICING_PER_MILLION.get(model)


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    cached_tokens: int,
    batch: bool = False,
) -> float | None:
    """USD cost for one call, or ``None`` if ``model`` has no pricing entry.

    ``cached_tokens`` are already counted inside ``input_tokens`` per the
    Gemini API contract, so the non-cached remainder bills at the input rate
    and the cached remainder at the (cheaper) cached rate. Thinking tokens
    bill as output, at the full output rate — that's how Google charges them.
    ``batch`` applies the batch-prediction discount to the whole call.
    """
    rates = _rates(model, input_tokens)
    if rates is None:
        return None
    billable_input = max(input_tokens - cached_tokens, 0)
    cost = (
        billable_input * rates["input"]
        + cached_tokens * rates["cached"]
        + (output_tokens + thinking_tokens) * rates["output"]
    ) / 1_000_000
    if batch:
        cost *= _BATCH_DISCOUNT
    return round(cost, 6)


# run_id -> running totals for that run. An entry lives until something calls
# ``reset_run_cost`` (``tools.run_costs.persist_run_cost`` always does, even on
# failure), so this map only stays bounded as long as every context that binds
# a run_id also flushes it. In the long-lived services they all do: the
# discovery/sweep cycles, tailoring, profile extraction, and the batch resume
# pass. A binding path added without a flush leaks one entry per invocation for
# the life of the process — the short-lived CLIs get away with it only because
# they exit.
#
# No lock: ``run_context`` binds before the ``asyncio.gather`` fan-out and each
# task copies the context at creation, so every concurrent scorer of one run
# accumulates under the same key — and asyncio never interleaves the plain dict
# mutations below, which have no await points.
_ACCUMULATORS: dict[str, dict[str, Any]] = {}


def _empty_totals() -> dict[str, Any]:
    return {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cached_tokens": 0,
        "cost_usd": 0.0,
        "by_step": {},
    }


def _accumulate(fields: dict[str, Any]) -> None:
    """Add one call's already-computed usage to its run's running total.

    No-op outside a run context: there is nothing to attribute the spend to,
    and a ``None`` key would silently pool every unattributed call together.
    """
    run_id = current_run_id()
    if run_id is None:
        return
    totals = _ACCUMULATORS.setdefault(run_id, _empty_totals())
    totals["calls"] += 1
    for key in ("input_tokens", "output_tokens", "thinking_tokens", "cached_tokens"):
        totals[key] += fields[key]
    # An unpriced model still contributes its calls and tokens; the
    # llm.call.pricing_unknown warning is what flags the missing dollars.
    cost = fields["cost_usd"] or 0.0
    totals["cost_usd"] = round(totals["cost_usd"] + cost, 6)
    step = totals["by_step"].setdefault(fields["step"], {"calls": 0, "cost_usd": 0.0})
    step["calls"] += 1
    step["cost_usd"] = round(step["cost_usd"] + cost, 6)


def run_cost_snapshot(run_id: str) -> dict[str, Any]:
    """Totals accumulated so far for ``run_id``; zeros when it spent nothing."""
    totals = _ACCUMULATORS.get(run_id)
    return copy.deepcopy(totals) if totals else _empty_totals()


def reset_run_cost(run_id: str) -> None:
    """Drop ``run_id``'s totals (after they've been banked in the ledger)."""
    _ACCUMULATORS.pop(run_id, None)


def record_llm_call(
    *,
    step: str,
    response: GenerateContentResponse,
    job_id: str | None = None,
    batch: bool = False,
) -> dict[str, Any]:
    """Log token usage + computed cost for one ``generate_content`` call.

    ``step`` identifies the call site (e.g. ``"matching.parse_jd"``,
    ``"matching.score"``, ``"profile.extract"``, ``"tailoring.objective"``).
    ``run_id``/``user_id`` ride along automatically via structlog's bound
    contextvars (see module docstring); ``job_id`` does not — the per-job
    loggers in this codebase bind it locally (``log.bind(job_id=...)``,
    scoped to that logger instance), which doesn't reach a separate logger
    like this one. Passing it explicitly is what makes "cost per
    application" (one job's parse + score + tailor calls) queryable, not
    just "cost per run" (a whole discovery/matching cycle).
    Returns the logged fields so callers/tests can assert on them.
    """
    usage = response.usage_metadata
    model = response.model_version or "unknown"
    input_tokens = (usage.prompt_token_count if usage else None) or 0
    output_tokens = (usage.candidates_token_count if usage else None) or 0
    thinking_tokens = (usage.thoughts_token_count if usage else None) or 0
    cached_tokens = (usage.cached_content_token_count if usage else None) or 0
    total_tokens = (usage.total_token_count if usage else None) or 0

    cost_usd = compute_cost_usd(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=thinking_tokens,
        cached_tokens=cached_tokens,
        batch=batch,
    )

    fields = {
        "step": step,
        "job_id": job_id,
        "model": model,
        "batch": batch,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
    }
    if cost_usd is None:
        log.warning("llm.call.pricing_unknown", **fields)
    else:
        log.info("llm.call", **fields)
    _accumulate(fields)
    return fields
