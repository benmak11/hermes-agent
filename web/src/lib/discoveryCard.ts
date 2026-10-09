// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { DiscoverySettings, PlanView } from "@/lib/types";

/**
 * What the Profile page's "What we do while you're away" card says, kept pure
 * so the plan-dependent wording is testable without a DOM.
 *
 * A trial runs both loops unattended for its first week only; after that the
 * server's tick runs nothing, whatever the toggles say, so the card must not
 * advertise a next run.
 */

const DAY_MS = 24 * 60 * 60 * 1000;

export function relPast(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const mins = Math.floor((now - new Date(iso).getTime()) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 48 * 60) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / (24 * 60))}d ago`;
}

export function relNext(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "on next visit";
  const mins = Math.floor((new Date(iso).getTime() - now) / 60_000);
  if (mins <= 0) return "due now";
  if (mins < 60) return `in ${mins}m`;
  if (mins < 48 * 60) return `in ${Math.floor(mins / 60)}h`;
  return `in ${Math.floor(mins / (24 * 60))}d`;
}

/**
 * Show the paid "Score jobs we found…" button and its cost sheet? Only on an
 * explicit paid plan: a trial's "Find new jobs" already scores, and an older
 * API that sends no plan gets the safe answer.
 */
export function showPaidScoring(plan: PlanView | null | undefined): boolean {
  return plan?.tier === "paid";
}

/** Is the scheduler allowed to run this user's loops? True when unknown. */
function autoActive(plan: PlanView | null | undefined): boolean {
  return plan ? plan.auto_active : true;
}

export type DiscoveryCardCopy = {
  /** A line about the trial's free week; null on paid or an older API. */
  trialNote: string | null;
  discoveryStatus: string;
  sweepStatus: string;
  /** The note under the action buttons. */
  findNote: string;
};

export function discoveryCardCopy(input: {
  plan: PlanView | null | undefined;
  settings: DiscoverySettings;
  lastDiscoveryAt?: string | null;
  nextDiscoveryAt?: string | null;
  lastSweepAt?: string | null;
  nextSweepAt?: string | null;
  now?: number;
}): DiscoveryCardCopy {
  const { plan, settings, now = Date.now() } = input;
  const active = autoActive(plan);
  const paused = "paused after your first week";

  let trialNote: string | null = null;
  if (plan?.tier === "trial") {
    if (plan.auto_active && plan.auto_until) {
      const left = Math.max(
        1,
        Math.ceil((new Date(plan.auto_until).getTime() - now) / DAY_MS),
      );
      trialNote = `Daily search is included in your first week — ${left} day${left === 1 ? "" : "s"} left.`;
    } else if (!plan.auto_active) {
      trialNote =
        "Your free week of daily searches has ended — use Find new jobs anytime.";
    }
  }

  const discoveryStatus = !settings.auto_discovery
    ? "off — use Find new jobs below"
    : active
      ? `last ${relPast(input.lastDiscoveryAt, now)} · next ${relNext(input.nextDiscoveryAt, now)}`
      : `last ${relPast(input.lastDiscoveryAt, now)} · ${paused}`;

  const sweepStatus = !settings.liveness_sweep
    ? "off — taken-down postings stay until acted on"
    : active
      ? `last ${relPast(input.lastSweepAt, now)} · next ${relNext(input.nextSweepAt, now)}`
      : `last ${relPast(input.lastSweepAt, now)} · ${paused}`;

  const findNote = showPaidScoring(plan)
    ? "Finding is free. Scoring uses AI and costs money — we'll show you what it would cost before anything runs."
    : plan?.tier === "trial"
      ? `Find new jobs also scores up to ${plan.scoring_per_day} new matches a day, free during your trial.`
      : "Find new jobs searches the job boards for new openings.";

  return { trialNote, discoveryStatus, sweepStatus, findNote };
}
