# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The agent, end to end, against the real model. **This test costs money.**

Nothing here is mocked: it builds a real ``Runner`` around the real
``root_agent`` and streams a real Gemini response on the live GCP project.

So it carries the ``billed`` marker and ``pyproject.toml`` deselects that marker
by default — ``pytest tests/integration`` runs everything *except* this. To run
it deliberately::

    uv run pytest tests/integration -m billed

Budget roughly $0.01-0.02 for the pair of billed tests in this directory, and
expect ``429 RESOURCE_EXHAUSTED`` to mean production quota rather than a broken
test.
"""

import pytest
from coordinator.agent import root_agent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types


@pytest.mark.billed
def test_agent_stream() -> None:
    """
    Integration test for the agent stream functionality.
    Tests that the agent returns valid streaming responses.
    """

    session_service = InMemorySessionService()

    session = session_service.create_session_sync(user_id="test_user", app_name="test")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="test")

    message = types.Content(
        role="user", parts=[types.Part.from_text(text="Why is the sky blue?")]
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )
    assert len(events) > 0, "Expected at least one message"

    has_text_content = False
    for event in events:
        if (
            event.content
            and event.content.parts
            and any(part.text for part in event.content.parts)
        ):
            has_text_content = True
            break
    assert has_text_content, "Expected at least one message with text content"
