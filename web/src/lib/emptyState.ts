// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/** The queue's empty-state choice, kept pure so it is testable without a DOM. */

export type EmptyQueueState = {
  icon: string;
  tone: "muted" | "good";
  heading: string;
  /** Null means the threshold copy, which the page renders with the min score. */
  body: string | null;
  action: "lower" | null;
};

/**
 * An empty queue has three causes and only one of them is the threshold.
 * Offering "Lower threshold" to an account with no jobs at all — a new invite,
 * or one whose data was wiped — sends them to a control that cannot help.
 *
 * `pendingTotal` picks the state but is never printed: the unscored backlog
 * is deep by design, and a large number there reads as failure.
 */
export function emptyQueueState(
  pendingTotal: number | null,
  scoredTotal: number | null,
): EmptyQueueState {
  // Null counts mean an older API that doesn't report them; fall back to the
  // threshold copy rather than inventing a state we can't actually observe.
  const nothingDiscovered = pendingTotal === 0;
  const nothingScoredYet =
    pendingTotal !== null &&
    pendingTotal > 0 &&
    scoredTotal !== null &&
    scoredTotal === 0;

  return nothingDiscovered
    ? {
        icon: "◔",
        tone: "muted",
        heading: "No jobs yet",
        body: "Nothing has been discovered for this account yet. Discovery runs on a schedule, or you can start one from Companies.",
        action: null,
      }
    : nothingScoredYet
      ? {
          icon: "◔",
          tone: "muted",
          heading: "Scoring in progress",
          body: "We've found jobs for you and they're waiting to be scored. Scored jobs will appear here.",
          action: null,
        }
      : {
          icon: "✓",
          tone: "good",
          heading: "You're all caught up",
          body: null,
          action: "lower",
        };
}
