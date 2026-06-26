# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Batch runners for the deterministic pipelines.

Configuring logging here means every ``python -m cli.<runner>`` invocation gets
structured logs from the shared ``tools/`` pipelines (console format locally,
JSON when ``LOG_FORMAT=json``), without each runner having to wire it up. The
runners keep their human-friendly ``print`` summaries on top of this.
"""

from obs.logging import configure_logging

configure_logging()
