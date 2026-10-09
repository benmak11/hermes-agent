// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { relDays } from "@/app/app/applications/status";
import { ApiError } from "@/lib/apiError";
import type { Application, ApplicationStatus } from "@/lib/types";

/** Statuses where the user can still act: apply, edit, or retry. */
const ACTIONABLE: ApplicationStatus[] = ["ready_for_review", "failed"];

export type ReviewActions = {
  /** Apply on the employer's site, copy objective, "I applied". */
  showManual: boolean;
  /** Approve/Retry Submit. Only on an explicit `auto_submit: true`. */
  showSubmit: boolean;
  submitLabel: string;
  /** The objective section renders at all (nothing to show while tailoring). */
  showObjective: boolean;
  objectiveEditable: boolean;
  showDownload: boolean;
  /** Heading of the submission panel, or null when the panel is hidden. */
  panelTitle: string | null;
};

/**
 * What the review page offers for this application. `submitUnavailable` is set
 * after a submit came back `auto_submit_unavailable` (a page older than the
 * server's flag), and hides Submit without waiting for a refetch.
 */
export function reviewActions(
  app: Application,
  opts: { submitUnavailable?: boolean; now?: number } = {},
): ReviewActions {
  const actionable = ACTIONABLE.includes(app.status);
  return {
    showManual: actionable,
    showSubmit: actionable && app.auto_submit === true && !opts.submitUnavailable,
    submitLabel: app.status === "failed" ? "Retry Submit" : "Approve & Submit",
    showObjective: app.status !== "queued" && app.status !== "tailoring",
    objectiveEditable: actionable,
    showDownload: actionable && !!app.resume_variant_uri,
    panelTitle: panelTitle(app, opts.now ?? Date.now()),
  };
}

function panelTitle(app: Application, now: number): string | null {
  switch (app.status) {
    case "submitting":
      return "Sending it in…";
    case "submitted": {
      if (app.confirmation?.method !== "manual") return "Application sent ✓";
      const when = relDays(app.confirmation.submitted_at, now);
      return when ? `You applied ✓ · ${when}` : "You applied ✓";
    }
    case "responded":
      return "They replied";
    case "failed":
      return "That one didn't go through";
    case "posting_removed":
      return "This posting was taken down";
    default:
      return null;
  }
}

/** Plain-language body of the `failed` panel; never the raw error note. */
export function failedMessage(app: Application): string {
  return app.last_submitted_at
    ? "We couldn't send this one for you. You can still apply on the employer's site with the résumé we wrote, then mark it as applied here."
    : "Something went wrong while we were preparing this one. Try Regenerate, or apply on the employer's site yourself.";
}

/** The structured `detail.reason` of an API error, when it has one. */
function reason(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  try {
    const detail = (JSON.parse(err.body) as { detail?: unknown }).detail as
      | { reason?: unknown }
      | null
      | undefined;
    return typeof detail?.reason === "string" ? detail.reason : null;
  } catch {
    return null;
  }
}

function status(err: unknown): number | null {
  return err instanceof ApiError ? err.status : null;
}

const NOT_FOUND = "We couldn't find this application. It may have been removed.";

export function markAppliedErrorMessage(err: unknown): string {
  if (status(err) === 404) return NOT_FOUND;
  if (status(err) === 409) {
    return reason(err) === "already_submitted"
      ? "Hermes already sent this application, so there's nothing to mark."
      : "This application can't be marked as applied right now. Reload the page to see where it stands.";
  }
  return "Couldn't mark this as applied. Try again.";
}

export function submitErrorMessage(err: unknown): string {
  if (status(err) === 404) return NOT_FOUND;
  if (reason(err) === "auto_submit_unavailable") {
    return "Hermes can't send this one for you. Apply on the employer's site, then mark it as applied here.";
  }
  if (status(err) === 409) {
    return "This application can't be sent right now. Reload the page to see where it stands.";
  }
  if (status(err) === 503) return "Couldn't start sending it. Try again in a moment.";
  return "Couldn't send this application. Try again.";
}

/** True when a submit failure means "apply yourself" rather than "retry". */
export function isSubmitUnavailable(err: unknown): boolean {
  return reason(err) === "auto_submit_unavailable";
}

export function objectiveErrorMessage(err: unknown): string {
  if (status(err) === 404) return NOT_FOUND;
  if (status(err) === 409) return "This application can't be edited any more.";
  if (status(err) === 502) {
    return "Couldn't update your résumé; your objective wasn't changed. Try again.";
  }
  return "Couldn't save your objective. Try again.";
}

export function loadErrorMessage(err: unknown): string {
  if (status(err) === 404) return NOT_FOUND;
  return "Couldn't load this application. Try reloading the page.";
}
