# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""Submitters: drive a real ATS form, and report progress while doing it.

The one thing every submitter shares with its caller is the progress protocol —
``(message, status)`` pairs — so the one token that carries *meaning* rather
than display text lives here, where both halves can import it without either
depending on the other.
"""

from __future__ import annotations

#: The progress token a submitter emits **immediately before it clicks Submit**,
#: and the only one that is not a display label.
#:
#: Every other token a submitter emits is chatter for the timeline: "Opening
#: ...", "Attaching resume", and — until this constant existed — the click
#: itself, which used the same ``"submitting"`` token as the six steps around
#: it and was therefore indistinguishable from them. That mattered because it is
#: the **point of no return**: everything before it can be retried for free, and
#: everything after it may already be a real job application sitting in a real
#: company's ATS.
#:
#: ``run_submission``'s ``progress`` callback recognises this token and writes
#: ``submit_attempted_at`` onto the application. That field is what
#: ``tools.applications.reaper`` reads to decide whether a submission that died
#: without reporting an outcome may be handed back for retry or must be parked
#: as uncertain. Nothing else may write it: it means "a browser clicked Submit
#: on this document", and only the code standing next to the click knows that.
#:
#: **The timeline entry is still recorded as ``"submitting"``.** ``web/`` renders
#: statuses from a closed union and filters the submission timeline on
#: ``["submitting", "submitted", "failed"]``; a new token reaching the document
#: would render as an unknown. So this token names an *event* on the wire
#: between submitter and caller, and never reaches Firestore.
SUBMIT_CLICKED = "submit_clicked"
