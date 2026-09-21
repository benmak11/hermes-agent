// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import type { Application, ApplicationStatus } from "@/lib/types";

// Both application mappers (the review header pill and the tracking strip)
// live here so they can be unit-tested; the two pages only render the values.

const SAGE_PILL = { bg: "var(--sage-tint)", border: "#cfe0c8", color: "var(--sage)" };
const HONEY_PILL = { bg: "var(--honey-tint)", border: "#f4dfb4", color: "#9a6216" };
const BRICK_PILL = { bg: "#fbeae6", border: "#f0c8bd", color: "var(--brick)" };
const MUTED_PILL = { bg: "#f6ede1", border: "#e8dacb", color: "var(--ink-4)" };

export type StatusPill = {
  label: string;
  bg: string;
  border: string;
  color: string;
};

/** Visual treatment for each application status, reusing the warm tokens. */
export function statusPill(status: ApplicationStatus): StatusPill {
  switch (status) {
    case "ready_for_review":
      return { label: "ready for you to check", ...SAGE_PILL };
    case "submitted":
      return { label: "applied", ...SAGE_PILL };
    case "responded":
      return { label: "they replied", ...HONEY_PILL };
    case "failed":
      return { label: "needs you", ...BRICK_PILL };
    case "posting_removed":
      return { label: "posting removed", ...BRICK_PILL };
    case "tailoring":
    case "queued":
      return { label: "writing your resume…", ...MUTED_PILL };
    case "submitting":
      return { label: "sending it in…", ...MUTED_PILL };
    default:
      return { label: status, ...MUTED_PILL };
  }
}

/* ---------- pipeline strip (tracking page) ---------- */

export type Segment = { color: string; pulse?: boolean };

export type PipelineView = {
  segments: Segment[];
  label: string;
  labelColor: string;
  /** When set, the strip label links to the application review page. */
  labelHref?: string;
  pill?: { text: string; bg: string; border: string; color: string };
  /** Card-level tint (arrival / response states). */
  card?: { bg: string; border: string };
  rightNote?: { text: string; color: string };
};

/** Fill of a strip segment nothing has reached yet. */
export const IDLE = "#f0e3d3";

/** How recently an application must have been created to get the arrival tint. */
export const JUST_APPROVED_MS = 2 * 60 * 1000;

export function relDays(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const days = Math.floor((now - new Date(iso).getTime()) / 86_400_000);
  if (days <= 0) return "today";
  return `${days}d ago`;
}

export function pipelineView(app: Application, now = Date.now()): PipelineView {
  const good = "var(--sage)";
  const accent = "var(--terracotta-d)";
  const createdAt = app.timeline?.[0]?.at;
  const justApproved =
    createdAt && now - new Date(createdAt).getTime() < JUST_APPROVED_MS;
  const submittedAt =
    app.confirmation?.submitted_at ?? app.last_submitted_at ?? null;

  switch (app.status) {
    case "queued":
    case "tailoring":
      return {
        segments: [
          { color: good },
          { color: "var(--terracotta)", pulse: true },
          { color: IDLE },
          { color: IDLE },
        ],
        label: "writing your resume…",
        labelColor: accent,
        ...(justApproved
          ? {
              card: { bg: "#f6faf3", border: "#cfe0c8" },
              rightNote: { text: "you said yes ✓", color: good },
            }
          : {}),
      };
    case "ready_for_review":
      return {
        segments: [{ color: good }, { color: good }, { color: IDLE }, { color: IDLE }],
        label: "check it over →",
        labelColor: accent,
        labelHref: `/app/applications/${app.id}/review`,
        pill: { text: "ready for you to check", ...SAGE_PILL },
      };
    case "submitting":
      return {
        segments: [
          { color: good },
          { color: good },
          { color: "var(--terracotta)", pulse: true },
          { color: IDLE },
        ],
        label: "sending it in…",
        labelColor: accent,
      };
    case "submitted":
      return {
        segments: [{ color: good }, { color: good }, { color: good }, { color: IDLE }],
        label: "waiting to hear back",
        labelColor: "#a3927f",
        pill: {
          text: `applied${submittedAt ? ` · ${relDays(submittedAt, now)}` : ""}`,
          ...SAGE_PILL,
        },
      };
    case "responded":
      return {
        segments: [
          { color: good },
          { color: good },
          { color: good },
          { color: "var(--honey)" },
        ],
        label: "they replied",
        labelColor: "#9a6216",
        pill: { text: "★ recruiter replied", ...HONEY_PILL },
        card: { bg: "#fdf6e8", border: "#f4dfb4" },
      };
    case "posting_removed": {
      // The listing was taken down before we could submit — dismissed by the
      // agent, nothing to retry. Which segment died mirrors the failed case.
      const deadSeg = app.last_submitted_at ? 2 : 1;
      return {
        segments: [0, 1, 2, 3].map((i) => ({
          color: i < deadSeg ? good : i === deadSeg ? "var(--brick)" : IDLE,
        })),
        label: "listing taken down — dismissed",
        labelColor: "var(--ink-4)",
        pill: { text: "posting removed", ...BRICK_PILL },
      };
    }
    case "failed":
    default: {
      // Failed after a submit attempt → the applied segment broke; otherwise
      // tailoring did.
      const failedSeg = app.last_submitted_at ? 2 : 1;
      return {
        segments: [0, 1, 2, 3].map((i) => ({
          color:
            i < failedSeg ? good : i === failedSeg ? "var(--brick)" : IDLE,
        })),
        label: "open it to fix →",
        labelColor: "var(--brick)",
        labelHref: `/app/applications/${app.id}/review`,
        pill: { text: "needs you", ...BRICK_PILL },
        card: { bg: "var(--surface-warm)", border: "#f0c8bd" },
      };
    }
  }
}
