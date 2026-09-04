# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""The only end-to-end proof that the gateway actually boots.

Everything else about ``api.main`` is tested in-process against a TestClient.
This file is the one place a *real* uvicorn subprocess imports ``api.main:app``,
serves an authenticated route, and is asserted on — which is what catches an
import-time or middleware-wiring break that a TestClient constructed inside an
already-working interpreter would not.

It costs nothing to run: the one request it makes writes a log line. No test
here drives a model, and ``tests/unit/test_local_guards.py`` asserts that stays
true.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from requests.exceptions import RequestException

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_URL = "http://127.0.0.1:8000"
FEEDBACK_URL = BASE_URL + "/feedback"

HEADERS = {"Content-Type": "application/json"}


def log_output(pipe: Any, log_func: Any) -> None:
    """Log the output from the given pipe."""
    for line in iter(pipe.readline, ""):
        log_func(line.strip())


def start_server() -> subprocess.Popen[str]:
    """Start the FastAPI server using subprocess and log its output."""
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "api.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    env = os.environ.copy()
    env["INTEGRATION_TEST"] = "TRUE"
    # /feedback now requires a verified caller. Use the local auth bypass rather
    # than minting a real Firebase token for a loopback server.
    env["AUTH_DEV_MODE"] = "1"
    env.setdefault("AUTH_DEV_USER", "integration-test-user")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    # Start threads to log stdout and stderr in real-time
    threading.Thread(
        target=log_output, args=(process.stdout, logger.info), daemon=True
    ).start()
    threading.Thread(
        target=log_output, args=(process.stderr, logger.error), daemon=True
    ).start()

    return process


def wait_for_server(timeout: int = 90, interval: int = 1) -> bool:
    """Wait for the server to be ready.

    Probes ``/openapi.json``, which ``api.main`` publishes only when
    ``deps.dev_mode()`` is true — i.e. this works *because* ``start_server``
    sets ``AUTH_DEV_MODE=1``. Drop that env var and this poll times out rather
    than failing on the assertion, so the two move together.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get("http://127.0.0.1:8000/openapi.json", timeout=10)
            if response.status_code == 200:
                logger.info("Server is ready")
                return True
        except RequestException:
            pass
        time.sleep(interval)
    logger.error(f"Server did not become ready within {timeout} seconds")
    return False


@pytest.fixture(scope="session")
def server_fixture(request: Any) -> Iterator[subprocess.Popen[str]]:
    """Pytest fixture to start and stop the server for testing."""
    logger.info("Starting server process")
    server_process = start_server()
    if not wait_for_server():
        pytest.fail("Server failed to start")
    logger.info("Server process started")

    def stop_server() -> None:
        logger.info("Stopping server process")
        server_process.terminate()
        server_process.wait()
        logger.info("Server process stopped")

    request.addfinalizer(stop_server)
    yield server_process


def test_collect_feedback(server_fixture: subprocess.Popen[str]) -> None:
    """
    Test the feedback collection endpoint (/feedback) to ensure it properly
    logs the received feedback.
    """
    # Create sample feedback data. The client-supplied "user_id" is accepted by
    # the schema but discarded by the route — the logged uid comes from the auth
    # dependency (here, AUTH_DEV_USER via the bypass set in start_server).
    feedback_data = {
        "score": 4,
        "user_id": "test-user-456",
        "session_id": "test-session-456",
        "text": "Great response!",
    }

    response = requests.post(
        FEEDBACK_URL, json=feedback_data, headers=HEADERS, timeout=10
    )
    assert response.status_code == 200
