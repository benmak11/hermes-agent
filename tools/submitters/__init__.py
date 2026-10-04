# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Submitters: drive a real ATS form, and report progress while doing it.

The one thing every submitter shares with its caller is the progress protocol —
``(message, status)`` pairs — so the one token that carries *meaning* rather
than display text lives here, where both halves can import it without either
depending on the other.
"""

from __future__ import annotations

#: The progress token a submitter emits immediately before it clicks Submit,
#: and the only one that is not a display label. It marks the point of no
#: return: everything before it can be retried for free, everything after it
#: may already be a real application in a real company's ATS.
#:
#: ``run_submission``'s ``progress`` callback recognises it and writes
#: ``submit_attempted_at``, which ``tools.applications.reaper`` reads to decide
#: whether a dead submission may be retried. Nothing else may write that field:
#: only the code standing next to the click knows a browser clicked.
#:
#: The timeline entry is still recorded as ``"submitting"``, because ``web/``
#: renders statuses from a closed union and would show a new token as unknown.
#: This token names an event on the wire and never reaches Firestore.
SUBMIT_CLICKED = "submit_clicked"
