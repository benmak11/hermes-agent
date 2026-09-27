// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
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
  const measured = source.replace(/^measured_/, "").replace(/_/g, "-");
  return `no runs measured on your account yet — using our ${measured} measurement (${per})`;
}

/**
 * When a quote grants zero jobs, *which* cap is empty — and they behave
 * differently, so the copy must too.
 *
 * `remaining_day` rolls at midnight UTC. `remaining_cycle` does not roll on
 * any clock at all: only a new discovery cycle clears it (see
 * `budget.apply_reservation`, which spells this out — "even after the UTC day
 * has rolled"). "It resets tomorrow" is true of one and false of the other,
 * and saying it of the wrong one sends the user away for a day to find the
 * same zero.
 *
 * When both are empty the day is named: it is the longer wait, and opening a
 * fresh cycle would not help until it rolls.
 */
export function zeroGrantReason(caps: {
  remaining_cycle: number;
  remaining_day: number;
}): "day" | "cycle" {
  return caps.remaining_day === 0 ? "day" : "cycle";
}
