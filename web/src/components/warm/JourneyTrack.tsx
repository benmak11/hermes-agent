// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import { Fragment, type CSSProperties } from "react";
import type { TrackStage, TrackStatus } from "./journeyStages";

/**
 * Node/connector row from the marketing hero ("Hermes Website" design).
 * Warm tokens only; no font-family so it inherits whatever the host sets.
 * `fluid` shares the width across stages; `fixed` gives each a 112px column
 * for the horizontally-scrolling journey board (Warm Flow screen 16).
 */

const NOTE_TONE: Record<NonNullable<TrackStage["noteTone"]>, string> = {
  good: "var(--sage)",
  accent: "var(--terracotta)",
  muted: "var(--ink-4)",
};

/** Connector colour is owned by the node it leaves from. */
const CONNECTOR_BG: Record<TrackStatus, string> = {
  done: "var(--sage)",
  current: "var(--border-warm-hair)",
  upcoming: "#f4ebdf",
};

const NODE_BASE: CSSProperties = {
  width: 28,
  height: 28,
  borderRadius: "50%",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  boxSizing: "border-box",
  flex: "none",
};

function Node({ status }: { status: TrackStatus }) {
  if (status === "done") {
    return (
      <span
        aria-hidden="true"
        style={{
          ...NODE_BASE,
          background: "var(--sage)",
          color: "#f6faf3",
          fontSize: 13,
          fontWeight: 800,
        }}
      >
        ✓
      </span>
    );
  }
  if (status === "current") {
    return (
      <span
        aria-current="step"
        style={{
          ...NODE_BASE,
          background: "var(--terracotta-tint)",
          border: "2px solid var(--terracotta)",
          animation: "jpulse 2.6s ease-in-out infinite",
        }}
      >
        <span
          style={{
            width: 9,
            height: 9,
            borderRadius: "50%",
            background: "var(--terracotta)",
          }}
        />
      </span>
    );
  }
  return (
    <span
      aria-hidden="true"
      style={{
        ...NODE_BASE,
        background: "var(--surface-warm)",
        border: "1px dashed #d9c4a8",
      }}
    />
  );
}

const NAME_STYLE: Record<TrackStatus, CSSProperties> = {
  done: { fontWeight: 600, color: "var(--ink-2)" },
  current: { fontWeight: 800, color: "var(--terracotta)" },
  upcoming: { fontWeight: 600, color: "var(--ink-4)" },
};

export function JourneyTrack({
  stages,
  layout = "fluid",
}: {
  stages: TrackStage[];
  layout?: "fluid" | "fixed";
}) {
  const column: CSSProperties =
    layout === "fixed"
      ? { width: 112, flex: "none" }
      : { flex: 1, minWidth: 0 };
  return (
    <div role="list" style={{ display: "flex", alignItems: "flex-start" }}>
      {stages.map((stage, i) => (
        <Fragment key={stage.id}>
          <div
            role="listitem"
            style={{
              ...column,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 8,
            }}
          >
            <Node status={stage.status} />
            <span
              style={{
                fontSize: 12.5,
                textAlign: "center",
                ...NAME_STYLE[stage.status],
              }}
            >
              {stage.name}
            </span>
            {stage.note ? (
              <span
                style={{
                  fontSize: 11.5,
                  color: NOTE_TONE[stage.noteTone ?? "muted"],
                }}
              >
                {stage.note}
              </span>
            ) : null}
          </div>
          {i < stages.length - 1 ? (
            <div
              aria-hidden="true"
              style={{
                flex: 1,
                height: 2,
                marginTop: 13,
                background: CONNECTOR_BG[stage.status],
              }}
            />
          ) : null}
        </Fragment>
      ))}
    </div>
  );
}
