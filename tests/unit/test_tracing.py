# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Distributed tracing behind ``TRACE_REQUESTS``: off changes nothing; on,
one trace runs API request -> Cloud Tasks -> worker -> run -> stages.

Uses the real OpenTelemetry SDK with an in-memory exporter. The global tracer
provider can be set only once per process, so it is installed once here with a
sampler each test can swap.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.cloud import tasks_v2
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import (
    DEFAULT_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)
from opentelemetry.trace import SpanKind, StatusCode

import api.app_utils.telemetry as telemetry
import tools.matching.score as score
from api.app_utils.middleware import RequestContextMiddleware
from obs import tracing
from obs.logging import _add_trace_correlation, get_logger, run_context
from tools import queues

REPO_ROOT = Path(__file__).resolve().parents[2]
# Captured before the autouse fixture stubs it for every test.
_REAL_INSTRUMENT_GENAI = telemetry._instrument_genai

# A browser-supplied, sampled W3C header with a recognisable trace id.
_BROWSER_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
_BROWSER_TRACEPARENT = f"00-{_BROWSER_TRACE_ID}-b7ad6b7169203331-01"


class _SwitchSampler(Sampler):
    """Delegates to whichever sampler the current test chose."""

    def __init__(self) -> None:
        self.delegate: Sampler = DEFAULT_ON

    def should_sample(self, *args, **kwargs):
        return self.delegate.should_sample(*args, **kwargs)

    def get_description(self) -> str:
        return f"switch({self.delegate.get_description()})"


_EXPORTER = InMemorySpanExporter()
_SAMPLER = _SwitchSampler()
_PROVIDER: TracerProvider | None = None


def _install_provider() -> None:
    global _PROVIDER
    if _PROVIDER is None:
        assert not isinstance(trace.get_tracer_provider(), TracerProvider), (
            "another test installed a global TracerProvider first"
        )
        _PROVIDER = TracerProvider(sampler=_SAMPLER)
        _PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(_PROVIDER)
    assert trace.get_tracer_provider() is _PROVIDER


@pytest.fixture(autouse=True)
def spans(monkeypatch):
    """The in-memory exporter, emptied, with the default sampler restored."""
    _install_provider()
    _EXPORTER.clear()
    _SAMPLER.delegate = DEFAULT_ON
    saved_textmap = propagate.get_global_textmap()
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("WORKER_MODE", raising=False)
    structlog.contextvars.clear_contextvars()
    yield _EXPORTER
    structlog.contextvars.clear_contextvars()
    propagate.set_global_textmap(saved_textmap)


@pytest.fixture(autouse=True)
def genai_instrumented(monkeypatch):
    """Record instead of patching google-genai process-wide."""
    calls: list[bool] = []
    monkeypatch.setattr(telemetry, "_instrument_genai", lambda: calls.append(True))
    return calls


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("TRACE_REQUESTS", "1")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv("TRACE_REQUESTS", raising=False)


class _FakeTasksClient:
    def __init__(self):
        self.created: list[dict] = []

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def task_path(self, project, location, queue, task_id):
        return f"{self.queue_path(project, location, queue)}/tasks/{task_id}"

    def create_task(self, *, parent, task):
        self.created.append(task)


@pytest.fixture
def tasks(monkeypatch):
    """Queue mode against a fake Cloud Tasks client; returns created tasks."""
    monkeypatch.setenv("QUEUE_MODE", "1")
    monkeypatch.setenv("WORKER_URL", "https://worker.example.run.app")
    monkeypatch.setenv("TASKS_SA_EMAIL", "tasks@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("TASKS_LOCATION", "us-central1")
    client = _FakeTasksClient()
    monkeypatch.setattr(queues, "_client", lambda: (tasks_v2, client))
    return client.created


def _build_app() -> FastAPI:
    """The gateway's wiring order: tracing set up, then the request middleware."""
    app = FastAPI()
    telemetry.setup_request_tracing(app)
    app.add_middleware(RequestContextMiddleware)
    log = get_logger("test.route")

    @app.get("/echo")
    def echo() -> dict:
        log.info("route.ran")
        return {}

    @app.get("/health")
    def health() -> dict:
        return {}

    @app.post("/kick")
    def kick() -> dict:
        # GOOGLE_CLOUD_PROJECT is read by enqueue only, so set it just here.
        os.environ["GOOGLE_CLOUD_PROJECT"] = "proj"
        try:
            queues.enqueue("score", "/tasks/score", {"user_id": "u1"})
        finally:
            del os.environ["GOOGLE_CLOUD_PROJECT"]
        return {}

    @app.post("/tasks/score")
    def task() -> dict:
        bound = dict(structlog.contextvars.get_contextvars())
        with run_context("score_task", user_id="u1"):
            log.info("run.step")
        return bound

    return app


def _server_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER]


def _has_otel_middleware(app: FastAPI) -> bool:
    node = app.build_middleware_stack()
    while node is not None:
        if type(node).__name__ == "OpenTelemetryMiddleware":
            return True
        node = getattr(node, "app", None)
    return False


# ------------------------------------------------------------------ flag off


def test_off_installs_nothing(flag_off, genai_instrumented):
    before = propagate.get_global_textmap()
    app = FastAPI()

    assert telemetry.setup_request_tracing(app) is False

    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
    assert not _has_otel_middleware(app)
    assert propagate.get_global_textmap() is before
    assert genai_instrumented == []


def test_off_a_request_produces_no_span(flag_off, spans):
    client = TestClient(_build_app())

    client.get("/echo", headers={"traceparent": _BROWSER_TRACEPARENT})

    assert spans.get_finished_spans() == ()


def test_off_enqueue_adds_no_traceparent_even_inside_a_span(flag_off, tasks):
    os.environ["GOOGLE_CLOUD_PROJECT"] = "proj"
    try:
        with trace.get_tracer("test").start_as_current_span("ambient"):
            assert queues.enqueue("score", "/tasks/score", {"user_id": "u1"})
    finally:
        del os.environ["GOOGLE_CLOUD_PROJECT"]

    headers = tasks[0]["http_request"]["headers"]
    assert "traceparent" not in headers
    assert "tracestate" not in headers


def test_off_run_context_opens_no_span(flag_off, spans):
    with run_context("score_task", user_id="u1") as run_id:
        assert run_id
        assert not trace.get_current_span().get_span_context().is_valid
        with tracing.span("score.parse") as stage:
            assert stage is None

    assert spans.get_finished_spans() == ()


def test_off_imports_no_opentelemetry():
    """With the flag off the helpers must not even import the SDK."""
    code = (
        "import sys\n"
        "from obs import tracing\n"
        "from obs.logging import run_context\n"
        "import tools.queues\n"
        "with run_context('x'):\n"
        "    assert tracing.trace_headers() == {}\n"
        "    with tracing.span('y') as s:\n"
        "        assert s is None\n"
        "print(sorted(m for m in sys.modules if m.startswith('opentelemetry')))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "TRACE_REQUESTS"}
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


# ------------------------------------------------------------------- flag on


def test_on_instruments_the_app_and_genai(flag_on, genai_instrumented):
    app = FastAPI()

    assert telemetry.setup_request_tracing(app) is True

    assert _has_otel_middleware(app)
    assert genai_instrumented == [True]


def test_on_a_request_is_one_server_span_and_its_logs_carry_it(flag_on, spans):
    client = TestClient(_build_app())

    with structlog.testing.capture_logs(processors=[_add_trace_correlation]) as logs:
        assert client.get("/echo").status_code == 200

    finished = spans.get_finished_spans()
    # receive/send sub-spans are excluded: one billed span per request.
    assert len(finished) == 1
    (server,) = finished
    assert server.kind == SpanKind.SERVER
    trace_id = f"{server.context.trace_id:032x}"
    span_id = f"{server.context.span_id:016x}"
    # Both access-log lines from RequestContextMiddleware, and the route's own.
    opened = next(e for e in logs if e["event"] == "GET /echo")
    closed = next(e for e in logs if e["event"].startswith("GET /echo 200 "))
    routed = next(e for e in logs if e["event"] == "route.ran")
    for event, line in (("open", opened), ("close", closed), ("route", routed)):
        assert line["logging.googleapis.com/trace"] == trace_id, event
        assert line["logging.googleapis.com/spanId"] == span_id, event


@pytest.mark.parametrize("path", ["/health", "/healthz", "/readiness", "/liveness"])
def test_on_quiet_paths_produce_no_span(flag_on, spans, path):
    client = TestClient(_build_app())

    client.get(path)
    client.get("/echo")

    assert [s.attributes.get("http.route") for s in _server_spans(spans)] == ["/echo"]


def test_on_api_ignores_a_browser_traceparent(flag_on, spans):
    client = TestClient(_build_app())

    client.get("/echo", headers={"traceparent": _BROWSER_TRACEPARENT})

    (server,) = _server_spans(spans)
    assert server.parent is None
    assert f"{server.context.trace_id:032x}" != _BROWSER_TRACE_ID


def test_on_a_browser_cannot_force_sampling_on_the_api(flag_on, spans):
    # The API's own sampler says "never"; a sampled inbound header must not
    # override it.
    _SAMPLER.delegate = ParentBased(TraceIdRatioBased(0.0))
    client = TestClient(_build_app())

    client.get("/echo", headers={"traceparent": _BROWSER_TRACEPARENT})

    assert spans.get_finished_spans() == ()


def test_on_enqueue_injects_the_current_span(flag_on, tasks):
    os.environ["GOOGLE_CLOUD_PROJECT"] = "proj"
    try:
        with trace.get_tracer("test").start_as_current_span("ambient") as ambient:
            queues.enqueue("score", "/tasks/score", {"user_id": "u1"})
    finally:
        del os.environ["GOOGLE_CLOUD_PROJECT"]

    ctx = ambient.get_span_context()
    assert tasks[0]["http_request"]["headers"]["traceparent"] == (
        f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01"
    )


def test_on_enqueue_without_a_span_adds_nothing(flag_on, tasks):
    os.environ["GOOGLE_CLOUD_PROJECT"] = "proj"
    try:
        queues.enqueue("score", "/tasks/score", {"user_id": "u1"})
    finally:
        del os.environ["GOOGLE_CLOUD_PROJECT"]

    assert "traceparent" not in tasks[0]["http_request"]["headers"]


def test_end_to_end_api_request_to_worker_run_is_one_trace(
    flag_on, spans, tasks, monkeypatch
):
    api = TestClient(_build_app())
    assert api.post("/kick", headers={"X-Request-Id": "rid-click-1"}).status_code == 200
    (api_span,) = _server_spans(spans)
    headers = tasks[0]["http_request"]["headers"]
    api_trace = f"{api_span.context.trace_id:032x}"
    assert headers["traceparent"].split("-")[1] == api_trace
    # The origin correlation headers ride along exactly as before.
    assert headers["X-Origin-Request-Id"] == "rid-click-1"
    assert headers["Content-Type"] == "application/json"

    # The worker is another process running the same image with WORKER_MODE.
    spans.clear()
    monkeypatch.setenv("WORKER_MODE", "1")
    worker = TestClient(_build_app())
    bound = worker.post("/tasks/score", headers=headers).json()

    (worker_span,) = _server_spans(spans)
    assert worker_span.context.trace_id == api_span.context.trace_id
    assert worker_span.parent is not None
    assert worker_span.parent.span_id == api_span.context.span_id
    (run_span,) = [s for s in spans.get_finished_spans() if s.name.startswith("run ")]
    assert run_span.parent.span_id == worker_span.context.span_id
    # The worker still binds the origin id for its logs.
    assert bound["origin_request_id"] == "rid-click-1"
    assert bound["request_id"] != "rid-click-1"


# --------------------------------------------------------- run + stage spans


def test_run_context_records_an_exception_and_reraises(flag_on, spans):
    with pytest.raises(RuntimeError, match="boom"):
        with run_context("score_task"):
            raise RuntimeError("boom")

    (run_span,) = spans.get_finished_spans()
    assert run_span.name == "run score_task"
    assert run_span.status.status_code == StatusCode.ERROR
    assert [e.name for e in run_span.events] == ["exception"]


def _score_one_job(monkeypatch, *, match_raises: bool = False) -> dict:
    """Score one unparsed job through the real loop, inside a run."""
    from test_geo_shadow import _job, _KeepingRef, _match, _parsed, _profile

    tracer = trace.get_tracer("test.genai")
    profile = _profile()
    ref = _KeepingRef()

    async def fake_load(db, user_id, limit=None):
        return profile, [(ref, _job("j1"))]

    async def no_cache_hit(db, jd_raw):
        return None

    async def fake_parse(job):
        with tracer.start_as_current_span("gemini parse"):
            return _parsed()

    async def no_store(*a, **kw):
        return None

    async def fake_match(job, prof, cached_content=None):
        with tracer.start_as_current_span("gemini match"):
            if match_raises:
                raise RuntimeError("pro failed")
            return _match(85)

    async def no_cache(prof, *a, **kw):
        return None

    async def grant(db, user_id, wanted=None, *, cycle_id=None, limits=None):
        return score.budget.Reservation(
            granted=1,
            capped=False,
            remaining_cycle=0,
            remaining_day=0,
            cycle_id=cycle_id,
        )

    async def release(*a, **kw):
        return None

    monkeypatch.setattr(score, "load_profile_and_pending", fake_load)
    monkeypatch.setattr(score.jd_cache, "lookup", no_cache_hit)
    monkeypatch.setattr(score.jd_cache, "store", no_store)
    monkeypatch.setattr(score, "parse_jd", fake_parse)
    monkeypatch.setattr(score, "match_job", fake_match)
    monkeypatch.setattr(score, "create_match_cache", no_cache)
    monkeypatch.setattr(score.budget, "reserve", grant)
    monkeypatch.setattr(score.budget, "release", release)
    monkeypatch.setattr(score.firestore, "AsyncClient", lambda: None)

    async def run() -> dict:
        with run_context("score_task", user_id="u1"):
            return await score.score_pending_jobs("u1")

    return asyncio.run(run())


def test_stage_spans_nest_under_the_run_and_gemini_under_its_stage(
    flag_on, spans, monkeypatch
):
    counts = _score_one_job(monkeypatch)

    assert counts["scored"] == 1
    by_name: dict[str, list] = {}
    for s in spans.get_finished_spans():
        by_name.setdefault(s.name, []).append(s)
    (run_span,) = by_name["run score_task"]
    for stage in ("score.parse", "score.prefilter", "score.score", "score.persist"):
        (span,) = by_name[stage]
        assert span.parent.span_id == run_span.context.span_id, stage
        assert span.attributes["hermes.job_id"] == "j1"
    (parse,) = by_name["score.parse"]
    (pro,) = by_name["score.score"]
    assert by_name["gemini parse"][0].parent.span_id == parse.context.span_id
    assert by_name["gemini match"][0].parent.span_id == pro.context.span_id


def test_a_failing_stage_is_recorded_and_still_fails_the_job(
    flag_on, spans, monkeypatch
):
    counts = _score_one_job(monkeypatch, match_raises=True)

    # Propagated out of the stage to the loop's own handler, as before.
    assert counts["failed"] == 1 and counts["scored"] == 0
    (stage,) = [s for s in spans.get_finished_spans() if s.name == "score.score"]
    assert stage.status.status_code == StatusCode.ERROR
    (event,) = stage.events
    assert event.name == "exception"
    assert event.attributes["exception.message"] == "pro failed"


def test_off_the_scoring_loop_opens_no_span(flag_off, spans, monkeypatch):
    counts = _score_one_job(monkeypatch)

    assert counts["scored"] == 1
    # Only the fakes' own stand-in spans; none opened by the loop or the run.
    names = {s.name for s in spans.get_finished_spans()}
    assert names == {"gemini parse", "gemini match"}
    assert all(s.parent is None for s in spans.get_finished_spans())


# --------------------------------------------------------- genai instrumentor


def test_genai_instrumentation_emits_spans_only(monkeypatch):
    """No log events (they would bill as Cloud Logging) and no upload hook."""
    from opentelemetry._logs import NoOpLoggerProvider
    from opentelemetry.instrumentation.google_genai import GoogleGenAiSdkInstrumentor

    seen: dict = {}
    monkeypatch.setattr(
        GoogleGenAiSdkInstrumentor, "instrument", lambda self, **kw: seen.update(kw)
    )

    _REAL_INSTRUMENT_GENAI()

    assert isinstance(seen["logger_provider"], NoOpLoggerProvider)
    assert seen["completion_hook"].on_completion(anything=1) is None
