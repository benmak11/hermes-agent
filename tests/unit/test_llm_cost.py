# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Unit tests for LLM call cost computation (Phase 2 telemetry)."""

from google.genai import types

from obs.llm_cost import compute_cost_usd, record_llm_call


def _response(
    *,
    model_version: str = "gemini-3.1-pro-preview",
    prompt_token_count: int | None = 1000,
    candidates_token_count: int | None = 200,
    thoughts_token_count: int | None = 300,
    cached_content_token_count: int | None = 0,
    total_token_count: int | None = 1500,
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        model_version=model_version,
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_token_count,
            candidates_token_count=candidates_token_count,
            thoughts_token_count=thoughts_token_count,
            cached_content_token_count=cached_content_token_count,
            total_token_count=total_token_count,
        ),
    )


def test_compute_cost_usd_bills_thinking_as_output() -> None:
    # Pro: input $2/1M, output $12/1M. 1000 input, 200 output + 300 thinking.
    cost = compute_cost_usd(
        model="gemini-3.1-pro-preview",
        input_tokens=1000,
        output_tokens=200,
        thinking_tokens=300,
        cached_tokens=0,
    )
    expected = (1000 * 2.00 + 500 * 12.00) / 1_000_000
    assert cost == round(expected, 6)


def test_compute_cost_usd_discounts_cached_input() -> None:
    # 1000 input tokens, 400 of which are cached (cached rate $0.20/1M vs
    # full input rate $2.00/1M) — cached portion should be much cheaper.
    cost = compute_cost_usd(
        model="gemini-3.1-pro-preview",
        input_tokens=1000,
        output_tokens=0,
        thinking_tokens=0,
        cached_tokens=400,
    )
    expected = (600 * 2.00 + 400 * 0.20) / 1_000_000
    assert cost == round(expected, 6)


def test_compute_cost_usd_long_context_tier() -> None:
    # Above the 200K-token threshold, Pro switches to the long-context rates.
    cost = compute_cost_usd(
        model="gemini-3.1-pro-preview",
        input_tokens=250_000,
        output_tokens=100,
        thinking_tokens=0,
        cached_tokens=0,
    )
    expected = (250_000 * 4.00 + 100 * 18.00) / 1_000_000
    assert cost == round(expected, 6)


def test_compute_cost_usd_unknown_model_returns_none() -> None:
    assert (
        compute_cost_usd(
            model="some-future-model",
            input_tokens=100,
            output_tokens=10,
            thinking_tokens=0,
            cached_tokens=0,
        )
        is None
    )


def test_record_llm_call_extracts_all_token_fields() -> None:
    fields = record_llm_call(
        step="matching.score", response=_response(), job_id="job-1"
    )
    assert fields["step"] == "matching.score"
    assert fields["job_id"] == "job-1"
    assert fields["model"] == "gemini-3.1-pro-preview"
    assert fields["input_tokens"] == 1000
    assert fields["output_tokens"] == 200
    assert fields["thinking_tokens"] == 300
    assert fields["cached_tokens"] == 0
    assert fields["total_tokens"] == 1500
    assert fields["cost_usd"] == round((1000 * 2.00 + 500 * 12.00) / 1_000_000, 6)


def test_record_llm_call_handles_missing_usage_metadata() -> None:
    response = types.GenerateContentResponse(model_version="gemini-3.1-pro-preview")
    fields = record_llm_call(step="matching.score", response=response)
    assert fields["input_tokens"] == 0
    assert fields["output_tokens"] == 0
    assert fields["thinking_tokens"] == 0
    assert fields["cost_usd"] == 0.0


def test_record_llm_call_unknown_model_logs_none_cost() -> None:
    fields = record_llm_call(
        step="matching.parse_jd",
        response=_response(model_version="gemini-flash-mystery"),
    )
    assert fields["cost_usd"] is None
