# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The two things that must stay singular: the Vertex client, and the model ids.

Both used to be per-call-site copies — nine inline ``genai.Client(vertexai=True)``
constructions, and two separate declarations of ``FLASH_MODEL``/``PRO_MODEL``
kept aligned by a comment.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import tools.genai_client as genai_client

# ------------------------------------------------------------- the client memo


@pytest.fixture
def fake_clients(monkeypatch):
    """Count constructions without ever building a real Vertex client."""
    built: list[SimpleNamespace] = []

    def _fake(**kwargs):
        assert kwargs == {"vertexai": True}
        built.append(SimpleNamespace(n=len(built)))
        return built[-1]

    monkeypatch.setattr(genai_client.genai, "Client", _fake)
    return built


def test_one_client_is_reused_within_a_loop(fake_clients):
    async def main():
        return [genai_client.vertex_client() for _ in range(50)]

    clients = asyncio.run(main())

    assert len(fake_clients) == 1, "50 calls must not build 50 clients"
    assert all(c is clients[0] for c in clients)


def test_a_synchronous_caller_gets_a_client_too(fake_clients):
    """``tools.profile.extract`` calls the sync half with no loop running."""
    first = genai_client.vertex_client()
    assert genai_client.vertex_client() is first
    assert len(fake_clients) == 1


def test_a_new_event_loop_gets_a_new_client(fake_clients):
    """The cached httpx pool binds to one loop; handing it to another breaks it.

    Every ``cli/`` entry point runs its own ``asyncio.run``, so this is the case
    that decides whether the memo is safe at all.
    """
    a = asyncio.run(_get_client())
    b = asyncio.run(_get_client())

    assert a is not b
    assert len(fake_clients) == 2


async def _get_client():
    return genai_client.vertex_client()


def test_reset_forces_a_rebuild(fake_clients):
    """The seam ``conftest.reset_genai_client`` relies on."""
    first = genai_client.vertex_client()
    genai_client.reset_vertex_client()

    assert genai_client.vertex_client() is not first
    assert len(fake_clients) == 2


def test_the_memo_does_not_survive_between_tests():
    """Companion to the test above: the autouse reset really is in force.

    Without it, whichever test built a client first would keep handing it to
    every later test regardless of what that test patched — the exact shape that
    let ``no_production_firestore`` catch nothing for weeks.
    """
    assert genai_client._cached is None


def test_call_sites_go_through_the_shared_client(fake_clients):
    """All nine former inline constructions now resolve to one object."""
    import tools.matching.batch as batch
    import tools.matching.pipeline as pipeline
    import tools.profile.extract as extract
    import tools.tailoring.objective as objective

    for module in (pipeline, batch, objective, extract):
        assert module.vertex_client is genai_client.vertex_client

    async def main():
        return {id(genai_client.vertex_client()) for _ in range(4)}

    assert len(asyncio.run(main())) == 1
    assert len(fake_clients) == 1


# -------------------------------------------------------------- the model ids


def test_model_ids_have_exactly_one_home(monkeypatch):
    """``agents/_shared`` and the matching pipeline must resolve to one object.

    ``is``, not ``==``: two equal literals in two modules are two objects, which
    is precisely the state this replaced. Identity is what proves there is one
    declaration rather than two that currently agree.
    """
    # Set before the first import so _shared's ADC fallback never runs.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "unit-test-project")
    import _shared

    from tools import llm_models
    from tools.matching import pipeline

    assert _shared.FLASH_MODEL is llm_models.FLASH_MODEL
    assert _shared.PRO_MODEL is llm_models.PRO_MODEL
    assert _shared.COMPUTER_USE_MODEL is llm_models.COMPUTER_USE_MODEL
    assert pipeline.FLASH_MODEL is llm_models.FLASH_MODEL
    assert pipeline.PRO_MODEL is llm_models.PRO_MODEL


def test_model_ids_are_unchanged():
    """A de-duplication, not a retune.

    ``gemini-3.1-pro-preview`` is load-bearing: there is no bare
    ``gemini-3-pro`` in this project's Vertex catalog, and substituting one 404s
    every scoring call. Pinned so the move cannot have quietly changed a value.
    """
    from tools import llm_models

    assert llm_models.FLASH_MODEL == "gemini-flash-latest"
    assert llm_models.PRO_MODEL == "gemini-3.1-pro-preview"
    assert llm_models.COMPUTER_USE_MODEL == "gemini-3.1-pro-preview"


def test_llm_models_stays_import_free():
    """``agents/`` imports nothing else from ``tools/``; this edge must stay inert.

    No env mutation, no credential lookup, no third-party import — anything that
    could fail or block at import time would now do so inside the ADK agent
    loader.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "tools" / "llm_models.py"
    tree = ast.parse(source.read_text())

    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        # __future__ annotations is compile-time only.
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    ]
    assert imports == [], "tools/llm_models.py must import nothing"

    assignments = [n for n in tree.body if isinstance(n, ast.Assign)]
    assert len(assignments) == len(
        [n for n in tree.body if not isinstance(n, ast.Expr | ast.ImportFrom)]
    ), "tools/llm_models.py must contain only constant assignments"
