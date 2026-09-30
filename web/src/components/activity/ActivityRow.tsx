// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { CSSProperties } from "react";

import {
  activityView,
  type ActivityItem,
  type ActivityTone,
  type ActivityVisual,
} from "@/lib/activity";

/**
 * One leg of the product, rendered exactly as `activityView` describes it.
 *
 * **This component decides nothing.** `activityView` is already the contract
 * (nine states, one view each, unit-tested) and this file is the pixels for
 * it. In particular:
 *
 * - **Motion is driven off `visual`, never off `state`.** The three moving
 *   visuals — `shimmer`, `spinner`, `bar` — are reachable only from `running`
 *   and `queued`, so those are the only two states that move. `quiet` is a
 *   deliberately *still* marker for `waiting_external`, `failed` and
 *   `stalled`: the batch is with Google and **we are doing nothing**, so
 *   nothing here moves either. A moving bar is not an exception to the rule —
 *   its width is a measurement the server made, not a claim this file
 *   invented.
 * - **A bar is drawn only for `visual === "bar"`**, which the server reaches
 *   only when it measured both `done` and `total`. A numerator over an unknown
 *   denominator is a fabricated percentage, and that is the bug this whole
 *   surface exists to stop.
 * - **Nothing is estimated.** `elapsed` counts up from a real `since`, and
 *   `nextLine` quotes a real `next_at`. Neither is ever produced here.
 *
 * The animated classes live in `globals.css` (reduced-motion guarded, each
 * with a static fallback) and the properties they animate are never also set
 * inline — inline wins over a class and would silently kill the guard.
 */

const TONE_COLOR: Record<ActivityTone, string> = {
  active: "var(--terracotta)",
  waiting: "var(--honey)",
  idle: "#b0a08d",
  done: "var(--sage)",
  problem: "var(--brick)",
};

/** The still glyph for a state that is not moving. Never a spinner. */
const QUIET_GLYPH: Record<ActivityTone, string> = {
  active: "•",
  waiting: "◷", // a clock face: waiting, not working
  idle: "–",
  done: "✓",
  problem: "!",
};

const MARKER_BOX: CSSProperties = {
  width: 18,
  height: 18,
  flex: "none",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 12,
  fontWeight: 800,
  lineHeight: 1,
};

function Marker({ visual, tone }: { visual: ActivityVisual; tone: ActivityTone }) {
  const color = TONE_COLOR[tone];
  if (visual === "spinner") {
    return (
      <span aria-hidden="true" style={MARKER_BOX}>
        <span
          className="wm-act-spin"
          style={{
            width: 11,
            height: 11,
            borderRadius: "50%",
            // `border-color` is the animated class's business; only the hue
            // that varies by tone is set here.
            color,
          }}
        />
      </span>
    );
  }
  // Everything else — including `shimmer`, whose motion is the bar track, and
  // `none`, which still needs a placeholder so the rows line up — is a still
  // glyph. `none` gets a dot so faint it reads as absence.
  return (
    <span aria-hidden="true" style={{ ...MARKER_BOX, color }}>
      {visual === "none" ? QUIET_GLYPH.idle : QUIET_GLYPH[tone]}
    </span>
  );
}

const TRACK: CSSProperties = {
  height: 5,
  borderRadius: 999,
  background: "#f0e3d3",
  overflow: "hidden",
  marginTop: 6,
};

export function ActivityRow({ item, now }: { item: ActivityItem; now: number }) {
  const view = activityView(item, now);
  // Both numbers, or no bar — the server enforces the pair, `activityView`
  // enforces the visual, and this re-reads them only to size the fill.
  // `item.total` is checked for truth, not just for a number: a measured 0/0
  // is a real answer but not a drawable one, and 0% of nothing is the same
  // fabricated bar by another route.
  const pct =
    view.visual === "bar" && item.total
      ? Math.max(0, Math.min(100, Math.round(((item.done ?? 0) / item.total) * 100)))
      : null;

  return (
    <div
      data-kind={item.kind}
      data-state={item.state}
      data-visual={view.visual}
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        padding: "8px 0",
      }}
    >
      <Marker visual={view.visual} tone={view.tone} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: view.tone === "idle" ? "var(--ink-4)" : "var(--ink-2)",
          }}
        >
          {view.verb}
          {view.elapsed ? (
            <span style={{ fontWeight: 500, color: "var(--ink-4)" }}>
              {" · "}
              {view.elapsed}
            </span>
          ) : null}
        </div>
        {view.nextLine ? (
          <div style={{ fontSize: 11.5, color: "var(--ink-4)", marginTop: 2 }}>
            {view.nextLine}
          </div>
        ) : null}
        {view.visual === "shimmer" ? (
          // No denominator exists for discovery — it emits no incremental
          // board count — so this track carries movement and no percentage.
          <div style={TRACK}>
            <div className="wm-act-shimmer" style={{ height: "100%" }} />
          </div>
        ) : null}
        {pct !== null ? (
          <div style={TRACK}>
            <div
              className="wm-act-bar"
              data-bar-pct={pct}
              style={{ height: "100%", width: `${pct}%`, borderRadius: 999 }}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}
