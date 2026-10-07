// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import {
  allowanceLabel,
  ratingsResetLine,
  resetsLine,
  type ActivityResponse,
  type AllowanceBlock,
} from "@/lib/activity";
import { ActivityRow } from "@/components/activity/ActivityRow";

/**
 * "What Hermes is doing" — the server's own answer, rendered.
 *
 * This replaces the two components it deletes (`DiscoveryPill`,
 * `ScoringCard`), both of which asserted that work was underway from a
 * `localStorage` flag set at the end of onboarding. Neither had any idea
 * whether anything was running. Everything here comes from `GET /activity`,
 * which reports records only.
 *
 * Every leg the server sent is rendered, in the order it sent them. That order
 * and that completeness are deliberate on the server's side — "a client that
 * renders a fixed list cannot mistake 'this leg was omitted' for 'this leg is
 * idle'" — so the panel does not filter or re-sort. An idle leg reads as idle,
 * which is information.
 */

function AllowanceRow({
  block,
  noun,
  window,
  now,
  note,
}: {
  block: AllowanceBlock;
  noun: string;
  window: string;
  now: number;
  /** What to say under the figure. Defaults to when the window rolls. */
  note?: string;
}) {
  return (
    <div
      data-allowance={noun}
      style={{ display: "flex", alignItems: "baseline", gap: 8, minWidth: 0 }}
    >
      <span style={{ fontSize: 12.5, fontWeight: 700, color: "var(--ink-2)" }}>
        {allowanceLabel(block, noun, window)}
      </span>
      <span style={{ fontSize: 11.5, color: "var(--ink-4)" }}>
        {note ?? resetsLine(block.resets_at, now)}
      </span>
    </div>
  );
}

export function ActivityPanel({
  data,
  now,
}: {
  data: ActivityResponse;
  now: number;
}) {
  return (
    <section
      aria-label="What Hermes is doing"
      style={{
        borderRadius: 18,
        border: "1px solid var(--border-warm-hair)",
        background: "var(--surface-warm)",
        padding: "14px 18px",
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 800,
          letterSpacing: "0.14em",
          textTransform: "uppercase",
          color: "var(--ink-4)",
        }}
      >
        What Hermes is doing
      </div>

      <div style={{ marginTop: 6 }}>
        {data.items.map((item) => (
          <ActivityRow key={item.kind} item={item} now={now} />
        ))}
      </div>

      {/* The allowance: two caps, two windows, **two reset instants**. They
          coincide only on a Sunday, so each one carries its own. */}
      <div
        style={{
          marginTop: 10,
          paddingTop: 10,
          borderTop: "1px solid var(--border-warm-hair)",
          display: "flex",
          flexWrap: "wrap",
          gap: "6px 22px",
        }}
      >
        <AllowanceRow
          block={data.allowance.searches}
          noun="searches"
          window="this week"
          now={now}
        />
        <AllowanceRow
          block={data.allowance.ratings}
          noun="ratings"
          window="today"
          now={now}
          // Says "none left" ahead of the reset when this search's window is
          // empty though the day figure still shows room.
          note={ratingsResetLine(data.allowance.ratings, now)}
        />
      </div>
    </section>
  );
}
