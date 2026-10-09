// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { ActivityItem } from "@/lib/activity";
import type { PlanView } from "@/lib/types";

/** The queue's empty-state choice, kept pure so it is testable without a DOM. */

export type EmptyQueueState = {
  icon: string;
  tone: "muted" | "good";
  heading: string;
  /** Null means the threshold copy, which the page renders with the min score. */
  body: string | null;
  action: "lower" | null;
};

/** What is actually happening, from `GET /activity` and the user's plan. */
export type QueueContext = {
  /** A discovery run is queued or running right now. */
  searching: boolean;
  /** A scoring run (online or batch) is live right now. */
  scoring: boolean;
  plan: PlanView | null;
};

const IDLE: QueueContext = { searching: false, scoring: false, plan: null };

const LIVE = new Set(["queued", "running", "waiting_external"]);

/** The context `emptyQueueState` needs, from what the page already fetched. */
export function queueContext(
  items: ActivityItem[],
  plan: PlanView | null,
): QueueContext {
  const live = (kinds: string[]) =>
    items.some((i) => kinds.includes(i.kind) && LIVE.has(i.state));
  return {
    searching: live(["discovery"]),
    scoring: live(["scoring", "batch_scoring"]),
    plan,
  };
}

/**
 * An empty queue has three causes and only one of them is the threshold.
 * Offering "Lower threshold" to an account with no jobs at all — a new invite,
 * or one whose data was wiped — sends them to a control that cannot help.
 *
 * `pendingTotal` picks the state but is never printed: the unscored backlog
 * is deep by design, and a large number there reads as failure.
 *
 * Nothing claims work is happening unless `context` says it is, and nothing
 * promises a scheduled search the plan will not run.
 */
export function emptyQueueState(
  pendingTotal: number | null,
  scoredTotal: number | null,
  context: QueueContext = IDLE,
): EmptyQueueState {
  // Null counts mean an older API that doesn't report them; fall back to the
  // threshold copy rather than inventing a state we can't actually observe.
  const nothingDiscovered = pendingTotal === 0;
  const nothingScoredYet =
    pendingTotal !== null &&
    pendingTotal > 0 &&
    scoredTotal !== null &&
    scoredTotal === 0;

  if (nothingDiscovered) {
    return {
      icon: "◔",
      tone: "muted",
      heading: context.searching ? "Your first search is running" : "No jobs yet",
      body: context.searching
        ? "We're looking through job boards for roles that fit your profile. Matches appear here once they're scored."
        : noJobsBody(context.plan),
      action: null,
    };
  }
  if (nothingScoredYet) {
    if (context.scoring) {
      return {
        icon: "◔",
        tone: "muted",
        heading: "Scoring in progress",
        body: "We've found jobs for you and they're being scored now. Scored jobs will appear here.",
        action: null,
      };
    }
    return {
      icon: "◔",
      tone: "muted",
      heading: "Jobs found, not yet rated",
      body: notRatedBody(context.plan),
      action: null,
    };
  }
  return {
    icon: "✓",
    tone: "good",
    heading: "You're all caught up",
    body: null,
    action: "lower",
  };
}

function noJobsBody(plan: PlanView | null): string {
  if (plan?.tier === "trial" && plan.auto_active) {
    return "New matches arrive daily during your first week. After that, use Find new jobs on your Profile anytime.";
  }
  return "Use Find new jobs on your Profile to search for new openings.";
}

function notRatedBody(plan: PlanView | null): string {
  if (plan?.tier === "trial") {
    return plan.auto_active
      ? "We rate a few new jobs for you each day during your first week. New ratings will appear here."
      : "Use Find new jobs on your Profile to rate a few more each day.";
  }
  return "We've found jobs for you. Score them from your Profile to see matches here.";
}
