# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Bulk scoring through Vertex batch prediction.

The online scorer (``tools.matching.score``) is right for the scheduler's
small incremental runs; a backlog of thousands of jobs doesn't need answers
in seconds, and batch prediction bills at half the interactive rate on both
models. This module mirrors ``score_pending_jobs``'s outcome contract
(``match``/``jd_parsed`` persisted, low scores tombstoned into
``discarded_jobs``) but runs the LLM legs as two batch jobs: parse (Flash,
jobs missing ``jd_parsed``) then score (Pro, in-family jobs), with the free
family pre-filter applied locally in between.

Batch requests can't use the Phase 3.2 context cache (caches are an
interactive-API feature), so score requests inline the full static block —
the 50% batch discount on the whole call still beats the cache's ~30%.

Vertex batch I/O is GCS JSONL: requests upload to
``gs://<bucket>/vertex-batch/<run-tag>/{parse,score}/input.jsonl`` and output
lines echo each request next to its response. Responses join back to jobs by
that echoed request text; identical texts (reposted JDs) collapse into one
billed request whose response fans out to every matching job.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import UTC, datetime
from functools import cache

from google import genai
from google.cloud import firestore
from google.genai import _transformers, types
from pydantic import ValidationError

from models.job import Job, ParsedJD
from models.match import JobMatch
from obs.llm_cost import record_llm_call
from obs.logging import get_logger
from tools.matching.pipeline import (
    _MATCH_MAX_OUTPUT_TOKENS,
    _MATCH_THINKING,
    _PARSE_JD_THINKING,
    OUT_OF_FAMILY,
    PARSE_JD_PROMPT,
    PRO_MODEL,
    build_match_context,
    build_match_job_block,
)
from tools.matching.score import (
    OnResult,
    load_profile_and_pending,
    persist_jd_parsed,
    persist_result,
)

log = get_logger("tools.matching")

# Batch prediction rejects model *aliases* outright ("Do not support publisher
# model gemini-flash-latest" — verified live 2026-07-09, on both the global
# and regional endpoints), so the Flash leg pins the concrete model the alias
# currently serves. If the alias is ever repointed, update this and the
# matching obs/llm_cost.py pricing entry together. Concrete ids work fine on
# the global endpoint the rest of the app uses.
BATCH_FLASH_MODEL = "gemini-2.5-flash"
BATCH_PRO_MODEL = PRO_MODEL  # already a concrete id — batch accepts it as-is

_DONE_STATES = {
    types.JobState.JOB_STATE_SUCCEEDED,
    types.JobState.JOB_STATE_FAILED,
    types.JobState.JOB_STATE_CANCELLED,
    types.JobState.JOB_STATE_EXPIRED,
    types.JobState.JOB_STATE_PARTIALLY_SUCCEEDED,
}
# Partial success still delivers per-line responses for the lines that worked;
# the lines that didn't just count as failed jobs.
_USABLE_STATES = {
    types.JobState.JOB_STATE_SUCCEEDED,
    types.JobState.JOB_STATE_PARTIALLY_SUCCEEDED,
}

# Firestore writes per batch of results — same ballpark as the online scorer's
# LLM concurrency, but these are cheap document writes, not model calls.
_PERSIST_CONCURRENCY = 16


def batch_bucket_name() -> str:
    """Resolve the batch-I/O bucket (env override or <project>-staging)."""
    explicit = os.environ.get("BATCH_BUCKET")
    if explicit:
        return explicit
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "Set BATCH_BUCKET or GOOGLE_CLOUD_PROJECT to resolve the batch bucket."
        )
    return f"{project}-staging"


@cache
def _parse_generation_config() -> str:
    """REST ``generationConfig`` for a parse request, as a JSON string.

    Mirrors the interactive config in ``pipeline.parse_jd`` exactly (minus
    ``system_instruction``, which is a request-level sibling in REST). Cached
    as a string so building thousands of request lines doesn't redo the
    pydantic → REST schema conversion, and JSON round-tripped by the caller so
    each line owns its dict.
    """
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        # t_schema is the SDK-private converter the interactive path runs on
        # our pydantic models internally; using it keeps the batch schema
        # byte-identical to what online calls send. Pinned by unit test.
        response_schema=_transformers.t_schema(None, ParsedJD),
        temperature=0.1,
        thinking_config=_PARSE_JD_THINKING,
    )
    return cfg.model_dump_json(by_alias=True, exclude_none=True)


@cache
def _score_generation_config() -> str:
    """REST ``generationConfig`` for a score request — see above."""
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=_transformers.t_schema(None, JobMatch),
        temperature=0.2,
        thinking_config=_MATCH_THINKING,
        max_output_tokens=_MATCH_MAX_OUTPUT_TOKENS,
    )
    return cfg.model_dump_json(by_alias=True, exclude_none=True)


def build_parse_request(jd_raw: str) -> dict:
    """One JSONL line asking Flash for the structured JD."""
    return {
        "request": {
            "contents": [{"role": "user", "parts": [{"text": jd_raw}]}],
            "systemInstruction": {"parts": [{"text": PARSE_JD_PROMPT}]},
            "generationConfig": json.loads(_parse_generation_config()),
        }
    }


def build_score_request(context: str, job_block: str) -> dict:
    """One JSONL line asking Pro to score a job against the static context."""
    return {
        "request": {
            "contents": [
                {"role": "user", "parts": [{"text": f"{context}\n\n{job_block}"}]}
            ],
            "generationConfig": json.loads(_score_generation_config()),
        }
    }


def _request_text(line: dict) -> str | None:
    """The prompt text a batch output line echoes back — the join key."""
    try:
        return line["request"]["contents"][0]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None


def _response_of(line: dict, model: str) -> types.GenerateContentResponse | None:
    """Parse one output line's response, or None when the line errored.

    Batch output carries fields the SDK's response model *forbids* as extras
    (per-candidate ``score``, seen live 2026-07-09), so this rebuilds just the
    shape downstream consumes — text, usage, model — rather than validating
    the raw line. ``modelVersion`` defaults to the requested model (error
    lines omit it) so ``record_llm_call``'s pricing lookup keeps working.
    """
    resp = line.get("response")
    if not isinstance(resp, dict):
        return None
    candidates = resp.get("candidates") or []
    slim = {
        "candidates": [
            {"content": c.get("content")} for c in candidates[:1] if isinstance(c, dict)
        ],
        "usageMetadata": resp.get("usageMetadata"),
        "modelVersion": resp.get("modelVersion") or model,
    }
    try:
        return types.GenerateContentResponse.model_validate(slim)
    except ValidationError:
        return None


async def _run_batch(
    *,
    model: str,
    lines: list[dict],
    gcs_dir: str,
    display_name: str,
    poll_seconds: int,
    timeout_seconds: float,
) -> list[dict]:
    """Upload requests, run one batch job to completion, return output lines."""
    from google.cloud import storage

    bucket_name, _, prefix = gcs_dir.removeprefix("gs://").partition("/")
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    payload = "\n".join(json.dumps(line) for line in lines)
    await asyncio.to_thread(
        bucket.blob(f"{prefix}/input.jsonl").upload_from_string,
        payload,
        content_type="application/jsonl",
    )

    client = genai.Client(vertexai=True)
    job = await client.aio.batches.create(
        model=model,
        src=f"{gcs_dir}/input.jsonl",
        config=types.CreateBatchJobConfig(
            display_name=display_name, dest=f"{gcs_dir}/output"
        ),
    )
    log.info(
        "matching.batch.job_created",
        name=job.name,
        model=model,
        requests=len(lines),
    )

    deadline = time.monotonic() + timeout_seconds
    while job.state not in _DONE_STATES:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Batch job {job.name} still {job.state} after "
                f"{timeout_seconds / 3600:.1f}h — inspect or cancel it in the "
                "Vertex console; its output can be ingested by a later run."
            )
        await asyncio.sleep(poll_seconds)
        try:
            job = await client.aio.batches.get(name=job.name)
        except Exception as e:
            # One flaky poll (network blip, truncated JSON body — seen live
            # 2026-07-10) must not kill an hours-long run; keep the last known
            # state and ask again next tick. The deadline still bounds us.
            log.warning("matching.batch.poll_retry", name=job.name, error=str(e)[:200])
    if job.state not in _USABLE_STATES:
        raise RuntimeError(f"Batch job {job.name} ended {job.state}: {job.error}")

    out_lines: list[dict] = []
    for blob in storage_client.list_blobs(bucket_name, prefix=f"{prefix}/output"):
        if not blob.name.endswith(".jsonl"):
            continue
        text = await asyncio.to_thread(blob.download_as_text)
        # JSONL's delimiter is strictly "\n" — echoed JD text can contain
        # U+2028/U+2029, which Vertex leaves unescaped and str.splitlines()
        # treats as line breaks, shattering a JSON line mid-string (crashed a
        # live run 2026-07-10).
        out_lines.extend(json.loads(raw) for raw in text.split("\n") if raw.strip())
    log.info(
        "matching.batch.job_done",
        name=job.name,
        state=str(job.state),
        responses=len(out_lines),
    )
    return out_lines


def join_parse_responses(
    out_lines: list[dict], by_text: dict[str, list[Job]]
) -> list[Job]:
    """Attach parsed JDs to jobs in place; returns the jobs that failed.

    ``by_text`` maps each unique ``jd_raw`` to every job sharing it — one
    request line per unique text, response fanned out to all of them.
    """
    for line in out_lines:
        text = _request_text(line)
        jobs = by_text.get(text) if text is not None else None
        if not jobs:
            log.warning("matching.batch.orphan_response", stage="parse")
            continue
        response = _response_of(line, BATCH_FLASH_MODEL)
        if response is None or not response.text:
            continue  # left unparsed → counted failed below
        record_llm_call(
            step="matching.parse_jd",
            response=response,
            job_id=jobs[0].id,
            batch=True,
        )
        try:
            parsed = ParsedJD.model_validate_json(response.text)
        except ValidationError:
            continue
        for job in jobs:
            job.jd_parsed = parsed
    return [j for jobs in by_text.values() for j in jobs if j.jd_parsed is None]


def join_score_responses(
    out_lines: list[dict], context: str, by_block: dict[str, list[Job]]
) -> tuple[dict[str, JobMatch], list[Job]]:
    """Match batch score responses back to jobs.

    Returns ``(matches_by_job_id, failed_jobs)``. ``by_block`` maps each
    unique per-job block to its jobs; the echoed prompt is
    ``context + "\\n\\n" + block``, so the context prefix is stripped to
    recover the key.
    """
    matches: dict[str, JobMatch] = {}
    prefix = f"{context}\n\n"
    for line in out_lines:
        text = _request_text(line)
        if text is None or not text.startswith(prefix):
            log.warning("matching.batch.orphan_response", stage="score")
            continue
        jobs = by_block.get(text.removeprefix(prefix))
        if not jobs:
            log.warning("matching.batch.orphan_response", stage="score")
            continue
        response = _response_of(line, BATCH_PRO_MODEL)
        if response is None or not response.text:
            continue
        record_llm_call(
            step="matching.score",
            response=response,
            job_id=jobs[0].id,
            batch=True,
        )
        try:
            match = JobMatch.model_validate_json(response.text)
        except ValidationError:
            continue
        for job in jobs:
            m = match.model_copy(deep=True)
            m.job_id = job.id
            matches[job.id] = m
    failed = [j for jobs in by_block.values() for j in jobs if j.id not in matches]
    return matches, failed


async def batch_score_pending_jobs(
    user_id: str,
    *,
    limit: int | None = None,
    poll_seconds: int = 60,
    timeout_hours: float = 25.0,
    on_result: OnResult | None = None,
) -> dict:
    """Batch-mode twin of ``score_pending_jobs`` — same counts contract.

    Expect minutes-to-hours of turnaround (Vertex targets 24h, hence the
    default timeout); worth it only on runs big enough that half-price beats
    waiting, which is why the CLI keeps online mode as the default.
    """
    db = firestore.AsyncClient()
    profile, pending = await load_profile_and_pending(db, user_id, limit)
    counts = {"scored": 0, "discarded": 0, "failed": 0, "pending": len(pending)}
    started = time.monotonic()
    run_tag = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    gcs_root = f"gs://{batch_bucket_name()}/vertex-batch/{run_tag}"
    log.info("matching.batch.start", pending=len(pending), gcs=gcs_root, mode="batch")
    if not pending:
        return counts

    ref_by_job_id = {job.id: ref for ref, job in pending}
    timeout_seconds = timeout_hours * 3600

    def _fail(job: Job, why: str) -> None:
        counts["failed"] += 1
        log.error("match.failed", job_id=job.id, company=job.company, error=why)
        if on_result:
            on_result(job, None, why)

    # Stage 1 — parse missing JDs with Flash, deduped on identical raw text.
    to_parse: dict[str, list[Job]] = {}
    for _, job in pending:
        if job.jd_parsed is not None:
            continue
        if not job.jd_raw.strip():
            # Vertex rejects empty model input per-line anyway ("Model input
            # cannot be empty", seen live); fail fast instead of shipping a
            # request that can only bounce.
            _fail(job, "empty jd_raw")
            continue
        to_parse.setdefault(job.jd_raw, []).append(job)
    if to_parse:
        out_lines = await _run_batch(
            model=BATCH_FLASH_MODEL,
            lines=[build_parse_request(text) for text in to_parse],
            gcs_dir=f"{gcs_root}/parse",
            display_name=f"hermes-parse-{run_tag}",
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
        for job in join_parse_responses(out_lines, to_parse):
            _fail(job, "parse_jd failed in batch")
        # Make the Flash spend durable before the Pro stage gets hours to
        # fail (or this process to die) — the next run then skips stage 1.
        parsed_now = [
            job
            for jobs in to_parse.values()
            for job in jobs
            if job.jd_parsed is not None
        ]
        psem = asyncio.Semaphore(_PERSIST_CONCURRENCY)

        async def _save_parse(job: Job) -> None:
            async with psem:
                await persist_jd_parsed(ref_by_job_id[job.id], job)

        await asyncio.gather(*(_save_parse(job) for job in parsed_now))
        log.info("matching.batch.parses_persisted", count=len(parsed_now))

    # Stage 2 — free local family pre-filter; out-of-family goes straight to
    # tombstones through the same persistence path the online scorer uses.
    targets = {f.lower() for f in profile.preferences.target_role_families}
    to_persist: list[tuple[Job, JobMatch]] = []
    to_score: list[Job] = []
    for _, job in pending:
        if job.jd_parsed is None:
            continue  # already counted failed in stage 1
        if job.jd_parsed.role_family not in targets:
            m = OUT_OF_FAMILY.model_copy()
            m.job_id = job.id
            to_persist.append((job, m))
        else:
            to_score.append(job)

    # Stage 3 — score in-family jobs with Pro.
    if to_score:
        context = build_match_context(profile)
        by_block: dict[str, list[Job]] = {}
        for job in to_score:
            by_block.setdefault(build_match_job_block(job), []).append(job)
        out_lines = await _run_batch(
            model=BATCH_PRO_MODEL,
            lines=[build_score_request(context, block) for block in by_block],
            gcs_dir=f"{gcs_root}/score",
            display_name=f"hermes-score-{run_tag}",
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
        matches, failed = join_score_responses(out_lines, context, by_block)
        for job in failed:
            _fail(job, "scoring failed in batch")
        to_persist.extend(
            (job, matches[job.id]) for job in to_score if job.id in matches
        )

    sem = asyncio.Semaphore(_PERSIST_CONCURRENCY)

    async def _persist(job: Job, match: JobMatch) -> None:
        async with sem:
            try:
                outcome = await persist_result(ref_by_job_id[job.id], job, match)
                counts[outcome] += 1
                if on_result:
                    on_result(job, match, None)
            except Exception as e:
                _fail(job, str(e))

    await asyncio.gather(*(_persist(job, match) for job, match in to_persist))

    log.info(
        "matching.batch.done",
        duration_ms=int((time.monotonic() - started) * 1000),
        **counts,
    )
    return counts
