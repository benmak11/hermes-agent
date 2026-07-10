# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Vertex batch prediction for bulk scoring (Phase 3.4 cost work)."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from google.genai import types

import tools.matching.batch as batch
from models.job import Job, ParsedJD
from models.match import JobMatch, ScoreBreakdown
from obs.llm_cost import compute_cost_usd, record_llm_call
from tools.matching import pipeline


def _job(job_id="j1", jd_raw="Build things at Acme.") -> Job:
    return Job(
        id=job_id,
        user_id="u1",
        source="greenhouse",
        source_id="123",
        company="Acme",
        title="Staff Software Engineer",
        url=f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        jd_raw=jd_raw,
        discovered_at=datetime.now(UTC),
    )


def _match_json(job_id="j1") -> str:
    return JobMatch(
        job_id=job_id,
        overall_score=80,
        breakdown=ScoreBreakdown(
            role_fit=80,
            qualifications_match=80,
            seniority_match=80,
            comp_alignment=80,
            deal_breaker_penalty=100,
        ),
        matched_strengths=[],
        gaps=[],
        red_flags_hit=[],
        reasoning="test",
        recommendation="apply",
    ).model_dump_json()


def _output_line(request_text: str, response_text: str) -> dict:
    """One batch output JSONL line: echoed request + REST-shaped response."""
    return {
        "request": {"contents": [{"role": "user", "parts": [{"text": request_text}]}]},
        "response": {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": response_text}]}}
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 50,
                "totalTokenCount": 150,
            },
        },
    }


def test_parse_request_mirrors_interactive_config():
    line = batch.build_parse_request("JD text here")
    req = line["request"]
    assert req["contents"][0]["parts"][0]["text"] == "JD text here"
    assert req["systemInstruction"]["parts"][0]["text"] == pipeline.PARSE_JD_PROMPT
    gen = req["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["thinkingConfig"] == {"thinkingBudget": 512}
    assert gen["temperature"] == 0.1
    assert "properties" in gen["responseSchema"]  # ParsedJD schema inlined
    json.dumps(line)  # JSONL-serializable end to end


def test_score_request_mirrors_interactive_config():
    line = batch.build_score_request("STATIC CONTEXT", "JOB BLOCK")
    req = line["request"]
    assert req["contents"][0]["parts"][0]["text"] == "STATIC CONTEXT\n\nJOB BLOCK"
    assert "systemInstruction" not in req
    gen = req["generationConfig"]
    assert gen["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}
    assert gen["maxOutputTokens"] == pipeline._MATCH_MAX_OUTPUT_TOKENS
    assert gen["temperature"] == 0.2
    assert "properties" in gen["responseSchema"]  # JobMatch schema inlined
    json.dumps(line)


def test_join_parse_responses_fans_out_to_duplicate_jds():
    # Two jobs sharing one jd_raw ride a single (single-billed) request line.
    a, b = _job("a"), _job("b")
    c = _job("c", jd_raw="Different JD.")
    by_text = {a.jd_raw: [a, b], c.jd_raw: [c]}
    parsed = ParsedJD(
        role_family="engineering", seniority="staff", summary="Build things."
    ).model_dump_json()
    out_lines = [_output_line(a.jd_raw, parsed)]  # nothing came back for c

    failed = batch.join_parse_responses(out_lines, by_text)

    assert a.jd_parsed is not None and b.jd_parsed is not None
    assert a.jd_parsed.role_family == "engineering"
    assert failed == [c]


def test_join_score_responses_strips_context_and_rekeys_job_id():
    context = "STATIC CONTEXT"
    a, b = _job("a"), _job("b", jd_raw="Different JD.")
    by_block = {"BLOCK A": [a], "BLOCK B": [b]}
    out_lines = [
        _output_line(f"{context}\n\nBLOCK A", _match_json("wrong-id")),
        _output_line(f"{context}\n\nBLOCK B", "{not json"),  # bad line → failed
    ]

    matches, failed = batch.join_score_responses(out_lines, context, by_block)

    assert set(matches) == {"a"}
    assert matches["a"].job_id == "a"  # model output's job_id overridden
    assert failed == [b]


def test_batch_pricing_is_half_the_interactive_rate():
    kwargs = {
        "model": "gemini-3.1-pro-preview",
        "input_tokens": 1000,
        "output_tokens": 200,
        "thinking_tokens": 300,
        "cached_tokens": 0,
    }
    assert compute_cost_usd(**kwargs, batch=True) == pytest.approx(
        compute_cost_usd(**kwargs) / 2
    )


def test_record_llm_call_carries_batch_flag():
    response = types.GenerateContentResponse(
        model_version="gemini-3.1-pro-preview",
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=50, total_token_count=150
        ),
        candidates=[
            types.Candidate(content=types.Content(parts=[types.Part(text="{}")]))
        ],
    )
    fields = record_llm_call(step="matching.score", response=response, batch=True)
    assert fields["batch"] is True
    assert fields["cost_usd"] == pytest.approx(
        record_llm_call(step="matching.score", response=response)["cost_usd"] / 2
    )


def test_run_batch_polls_to_success_and_reads_output(monkeypatch):
    lines_in = [batch.build_parse_request("JD text")]
    out_line = _output_line("JD text", "{}")
    uploaded = {}

    class _FakeBlob:
        def __init__(self, name):
            self.name = name

        def upload_from_string(self, payload, content_type=None):
            uploaded[self.name] = payload

        def download_as_text(self):
            return json.dumps(out_line) + "\n"

    class _FakeBucket:
        def blob(self, name):
            return _FakeBlob(name)

    class _FakeStorageClient:
        def bucket(self, name):
            assert name == "test-bucket"
            return _FakeBucket()

        def list_blobs(self, bucket_name, prefix=None):
            return [_FakeBlob(f"{prefix}/predictions.jsonl")]

    import google.cloud.storage as gcs

    monkeypatch.setattr(gcs, "Client", lambda: _FakeStorageClient())

    states = iter(
        [types.JobState.JOB_STATE_RUNNING, types.JobState.JOB_STATE_SUCCEEDED]
    )

    class _FakeBatches:
        async def create(self, *, model, src, config):
            assert src == "gs://test-bucket/vertex-batch/run1/parse/input.jsonl"
            assert config.dest == "gs://test-bucket/vertex-batch/run1/parse/output"
            return SimpleNamespace(name="batch/123", state=next(states), error=None)

        async def get(self, *, name):
            return SimpleNamespace(name=name, state=next(states), error=None)

    fake = SimpleNamespace(aio=SimpleNamespace(batches=_FakeBatches()))
    monkeypatch.setattr(batch.genai, "Client", lambda **kw: fake)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(batch.asyncio, "sleep", _no_sleep)

    out = asyncio.run(
        batch._run_batch(
            model="gemini-flash-latest",
            lines=lines_in,
            gcs_dir="gs://test-bucket/vertex-batch/run1/parse",
            display_name="test",
            poll_seconds=1,
            timeout_seconds=60,
        )
    )
    assert out == [out_line]
    assert uploaded["vertex-batch/run1/parse/input.jsonl"] == json.dumps(lines_in[0])


def test_run_batch_raises_on_failed_job(monkeypatch):
    class _FakeBlob:
        def upload_from_string(self, payload, content_type=None):
            pass

    class _FakeBucket:
        def blob(self, name):
            return _FakeBlob()

    class _FakeStorageClient:
        def bucket(self, name):
            return _FakeBucket()

    import google.cloud.storage as gcs

    monkeypatch.setattr(gcs, "Client", lambda: _FakeStorageClient())

    class _FakeBatches:
        async def create(self, *, model, src, config):
            return SimpleNamespace(
                name="batch/123",
                state=types.JobState.JOB_STATE_FAILED,
                error="quota",
            )

    fake = SimpleNamespace(aio=SimpleNamespace(batches=_FakeBatches()))
    monkeypatch.setattr(batch.genai, "Client", lambda **kw: fake)

    with pytest.raises(RuntimeError, match="quota"):
        asyncio.run(
            batch._run_batch(
                model="gemini-flash-latest",
                lines=[batch.build_parse_request("x")],
                gcs_dir="gs://b/vertex-batch/r/parse",
                display_name="test",
                poll_seconds=1,
                timeout_seconds=60,
            )
        )


def test_batch_bucket_name(monkeypatch):
    monkeypatch.setenv("BATCH_BUCKET", "explicit-bucket")
    assert batch.batch_bucket_name() == "explicit-bucket"
    monkeypatch.delenv("BATCH_BUCKET")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    assert batch.batch_bucket_name() == "proj-staging"
