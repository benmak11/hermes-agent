# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.
"""``cli.geo_replay``'s report: the tombstone census.

The replay itself streams Firestore and is exercised against the real thing.
What is unit-testable is the printing, which runs at the very end of a long
read — so an arithmetic error there costs the whole run.
"""

from __future__ import annotations

from cli.geo_replay import Replay, report


def test_a_history_of_only_free_rejects_does_not_divide_by_zero(capsys):
    """``pro_calls`` is the denominator of the capped-at-20 share, and it is
    zero for an account whose tombstones all scored 0 (out-of-family, never a
    Pro call). Dividing anyway raises at the end of the stream and takes the
    whole report with it."""
    r = Replay(user_id="u1", tombstones=4, tombstones_free=4, tombstones_capped=0)

    report(r, title="u1")

    out = capsys.readouterr().out
    assert "Pro calls in this history: 0" in out
    assert "out-of-family" in out
    # No share, because there is nothing to take a share of.
    assert "ceiling on what this gate can save" not in out


def test_the_census_does_not_claim_the_tombstones_are_unreplayable(capsys):
    """Tombstones have carried ``jd_parsed`` since ``score.discard_tombstone``
    started storing it, so the gate *can* be replayed over them — this run
    counts them instead."""
    r = Replay(user_id="u1", tombstones=10, tombstones_free=3, tombstones_capped=5)

    report(r, title="u1")

    out = capsys.readouterr().out
    assert "no jd_parsed" not in out
    assert "carry jd_parsed" in out
    assert "Pro calls in this history: 7" in out
