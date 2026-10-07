// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { resetsLine } from "@/lib/activity";
import { ApiError } from "@/lib/apiError";
import type { SpendConfirmation } from "@/lib/types";

/**
 * Read a 402 "I need to ask you first" out of a failed API call.
 *
 * The backend answers 402 rather than 409 on a paid route with no consent
 * token: 409 already means "wrong application state" on /submit and
 * /regenerate, and a client that cannot tell the two apart will eventually
 * retry the wrong one. `ApiError` already carries the raw body, so this is
 * only a parse — no change to `apiFetch` was needed.
 *
 * Returns null for anything that is not a well-formed 402, because the UI's
 * fallback for "something else went wrong" must stay the error path. A
 * malformed 402 rendered as a confirm sheet would ask the user to approve a
 * blank.
 */
export function spendConfirmation(err: unknown): SpendConfirmation | null {
  if (!(err instanceof ApiError) || err.status !== 402) return null;
  try {
    const detail = (JSON.parse(err.body) as { detail?: unknown }).detail as
      | SpendConfirmation
      | undefined;
    if (!detail?.needs_confirmation || !detail.confirm_token) return null;
    if (typeof detail.estimate?.units !== "number") return null;
    return detail;
  } catch {
    return null;
  }
}

/** "$1.20 – $2.40", or "$0.00" when the two ends round together. */
export function usdRange(low: number, high: number): string {
  const fmt = (n: number) => `$${n.toFixed(2)}`;
  return fmt(low) === fmt(high) ? fmt(low) : `${fmt(low)} – ${fmt(high)}`;
}

/**
 * Where the per-job rate came from, in the user's words.
 *
 * Never silently interchangeable: "your last 312 jobs" and "our 2026-08-23
 * measurement" are different claims about how much to trust the number, and
 * the user is entitled to know which one they are looking at.
 */
export function rateProvenance(source: string, sample: number, rate: number): string {
  const per = `$${rate.toFixed(4)}/job`;
  if (source === "your_last_runs") {
    return `based on your last ${sample.toLocaleString()} scored jobs (${per})`;
  }
  // Match the date rather than strip a known prefix: fallback sources name more
  // than the date ("measured_rated_2026_08_23"), and stripping "measured_"
  // from that renders "rated-2026-08-23" to the user.
  const date = source.match(/(\d{4})_(\d{2})_(\d{2})/);
  const measured = date ? `${date[1]}-${date[2]}-${date[3]}` : "reference";
  return `no runs measured on your account yet — using our ${measured} measurement (${per})`;
}

/**
 * When a quote grants zero jobs, *which* cap is empty.
 *
 * Both roll at midnight UTC: the per-search window rolls with the day (see
 * `budget.apply_reservation`). The difference is the copy — "today's budget"
 * versus "this search's budget" — and that within a day only the daily
 * counter is shared across searches.
 *
 * When both are empty the day is named, as the server's 429 does.
 */
export function zeroGrantReason(caps: {
  remaining_cycle: number;
  remaining_day: number;
}): "day" | "cycle" {
  return caps.remaining_day === 0 ? "day" : "cycle";
}

/** The 429 `POST /jobs/score` answers when nothing could be rated. */
export type ScoringCap = {
  reason: "scoring_cap";
  cap: "day" | "cycle";
  resets_at: string;
};

/**
 * Read a scoring-cap 429 out of a failed API call, or null.
 *
 * A cap is not a price: there is no token and nothing to confirm, only a time
 * at which the answer changes.
 */
export function scoringCap(err: unknown): ScoringCap | null {
  if (!(err instanceof ApiError) || err.status !== 429) return null;
  try {
    const detail = (JSON.parse(err.body) as { detail?: unknown }).detail as
      | ScoringCap
      | undefined;
    if (detail?.reason !== "scoring_cap") return null;
    if (detail.cap !== "day" && detail.cap !== "cycle") return null;
    if (typeof detail.resets_at !== "string") return null;
    return detail;
  } catch {
    return null;
  }
}

/** "Today's scoring budget is used up — it resets at 01:00." in local time. */
export function scoringCapMessage(cap: ScoringCap, now: number): string {
  const what = cap.cap === "day" ? "Today's" : "This search's";
  const reset = resetsLine(cap.resets_at, now);
  return (
    `${what} scoring budget is used up — nothing can be scored right now.` +
    (reset ? ` It ${reset}.` : "")
  );
}
