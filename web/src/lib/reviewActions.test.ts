import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/apiError";
import {
  failedMessage,
  isSubmitUnavailable,
  loadErrorMessage,
  markAppliedErrorMessage,
  objectiveErrorMessage,
  reviewActions,
  submitErrorMessage,
} from "@/lib/reviewActions";
import type { Application, ApplicationStatus } from "@/lib/types";

const ALL: ApplicationStatus[] = [
  "queued", "tailoring", "ready_for_review", "submitting",
  "submitted", "failed", "responded", "posting_removed",
];
const ACTIONABLE: ApplicationStatus[] = ["ready_for_review", "failed"];
const NOW = Date.UTC(2026, 8, 20, 12, 0, 0);

function app(status: ApplicationStatus, extra: Partial<Application> = {}): Application {
  return {
    id: "a1", user_id: "u", job_id: "j", job_company: "Acme", status,
    master_bullets: [], tailored_bullets: [], timeline: [], ...extra,
  };
}

function apiErr(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }), "req-1");
}

describe("reviewActions visibility", () => {
  it("shows Submit only for an explicit auto_submit: true in an actionable status", () => {
    for (const s of ALL) {
      for (const auto of [true, false, undefined]) {
        const a = reviewActions(app(s, { auto_submit: auto }), { now: NOW });
        expect(a.showSubmit).toBe(ACTIONABLE.includes(s) && auto === true);
      }
    }
  });

  it("treats a missing auto_submit (older API) as hidden", () => {
    const a = app("ready_for_review");
    delete a.auto_submit;
    expect(reviewActions(a).showSubmit).toBe(false);
  });

  it("hides Submit after the server said auto submit is unavailable", () => {
    const a = app("ready_for_review", { auto_submit: true });
    expect(reviewActions(a, { submitUnavailable: true }).showSubmit).toBe(false);
    expect(reviewActions(a, { submitUnavailable: true }).showManual).toBe(true);
  });

  it("shows the manual path in ready_for_review and failed whatever auto_submit says", () => {
    for (const s of ALL) {
      for (const auto of [true, false, undefined]) {
        const a = reviewActions(app(s, { auto_submit: auto }));
        expect(a.showManual).toBe(ACTIONABLE.includes(s));
      }
    }
  });

  it("makes the objective editable only in ready_for_review and failed", () => {
    for (const s of ALL) {
      expect(reviewActions(app(s)).objectiveEditable).toBe(ACTIONABLE.includes(s));
    }
    expect(reviewActions(app("submitted")).objectiveEditable).toBe(false);
  });

  it("hides the objective section entirely while tailoring", () => {
    expect(reviewActions(app("queued")).showObjective).toBe(false);
    expect(reviewActions(app("tailoring")).showObjective).toBe(false);
    expect(reviewActions(app("submitted")).showObjective).toBe(true);
  });

  it("offers the download only when there is a résumé and the user can act", () => {
    const uri = "gs://bucket/r.docx";
    expect(reviewActions(app("ready_for_review", { resume_variant_uri: uri })).showDownload).toBe(true);
    expect(reviewActions(app("failed", { resume_variant_uri: uri })).showDownload).toBe(true);
    expect(reviewActions(app("failed")).showDownload).toBe(false);
    expect(reviewActions(app("submitted", { resume_variant_uri: uri })).showDownload).toBe(false);
  });

  it("labels the submit button by status", () => {
    expect(reviewActions(app("ready_for_review")).submitLabel).toBe("Approve & Submit");
    expect(reviewActions(app("failed")).submitLabel).toBe("Retry Submit");
  });
});

describe("reviewActions panel title", () => {
  const twoDaysAgo = new Date(NOW - 2 * 86_400_000).toISOString();

  it("reads 'You applied' with a relative date for a manual confirmation", () => {
    const a = app("submitted", { confirmation: { submitted_at: twoDaysAgo, method: "manual" } });
    expect(reviewActions(a, { now: NOW }).panelTitle).toBe("You applied ✓ · 2d ago");
  });

  it("reads 'Application sent' for an auto confirmation and for an old doc with no method", () => {
    const auto = app("submitted", { confirmation: { submitted_at: twoDaysAgo, method: "auto" } });
    const legacy = app("submitted", { confirmation: { submitted_at: twoDaysAgo } });
    const none = app("submitted");
    for (const a of [auto, legacy, none]) {
      expect(reviewActions(a, { now: NOW }).panelTitle).toBe("Application sent ✓");
    }
  });

  it("has no panel before submission and a heading for every later status", () => {
    for (const s of ["queued", "tailoring", "ready_for_review"] as const) {
      expect(reviewActions(app(s)).panelTitle).toBeNull();
    }
    for (const s of ["submitting", "submitted", "failed", "responded", "posting_removed"] as const) {
      expect(reviewActions(app(s)).panelTitle).toBeTruthy();
    }
  });
});

describe("failedMessage", () => {
  it("is plain language, never the raw timeline note", () => {
    const note = "TimeoutError: page.click exceeded 30000ms";
    const timeline = [{ at: "2026-09-20T00:00:00Z", status: "failed", note }];
    for (const extra of [{ last_submitted_at: "2026-09-20T00:00:00Z" }, {}]) {
      const m = failedMessage(app("failed", { timeline, ...extra }));
      expect(m).not.toContain(note);
      expect(m).not.toMatch(/error|exception|timeout/i);
    }
  });
});

describe("error messages", () => {
  const raw = /\{|request|detail|\d{3}/;

  it("maps every mark-applied outcome to human words", () => {
    expect(markAppliedErrorMessage(apiErr(404, "application not found"))).toMatch(/couldn't find/);
    expect(markAppliedErrorMessage(apiErr(409, { reason: "already_submitted", current: "submitted" })))
      .toMatch(/already sent/);
    expect(markAppliedErrorMessage(apiErr(409, { reason: "cannot_mark_applied", current: "submitting" })))
      .toMatch(/can't be marked as applied/);
    expect(markAppliedErrorMessage(new Error("network down"))).toMatch(/Try again/);
    for (const e of [apiErr(404, "x"), apiErr(409, { reason: "already_submitted" }), apiErr(500, "boom"), new Error("x")]) {
      expect(markAppliedErrorMessage(e)).not.toMatch(raw);
    }
  });

  it("maps submit errors, telling auto_submit_unavailable apart from a wrong status", () => {
    const unavailable = apiErr(409, { reason: "auto_submit_unavailable", message: "no submitter" });
    const wrongStatus = apiErr(409, "cannot submit from status 'submitted'");
    expect(isSubmitUnavailable(unavailable)).toBe(true);
    expect(isSubmitUnavailable(wrongStatus)).toBe(false);
    expect(isSubmitUnavailable(new Error("x"))).toBe(false);
    expect(submitErrorMessage(unavailable)).toMatch(/Apply on the employer's site/);
    expect(submitErrorMessage(wrongStatus)).toMatch(/can't be sent right now/);
    expect(submitErrorMessage(apiErr(503, "could not schedule"))).toMatch(/Try again in a moment/);
    for (const e of [unavailable, wrongStatus, apiErr(500, "boom"), new Error("x")]) {
      expect(submitErrorMessage(e)).not.toMatch(raw);
    }
  });

  it("maps objective save errors", () => {
    expect(objectiveErrorMessage(apiErr(409, "cannot edit the objective in status 'submitted'")))
      .toBe("This application can't be edited any more.");
    expect(objectiveErrorMessage(apiErr(502, "could not rebuild the resume")))
      .toBe("Couldn't update your résumé; your objective wasn't changed. Try again.");
    expect(objectiveErrorMessage(new Error("x"))).toMatch(/Try again/);
  });

  it("survives a non-JSON error body", () => {
    const html = new ApiError(409, "<html>bad gateway</html>", "req-1");
    expect(submitErrorMessage(html)).toMatch(/can't be sent right now/);
    expect(markAppliedErrorMessage(html)).toMatch(/can't be marked as applied/);
  });

  it("maps load errors", () => {
    expect(loadErrorMessage(apiErr(404, "application not found"))).toMatch(/couldn't find/);
    expect(loadErrorMessage(new Error("x"))).not.toMatch(raw);
  });
});
