import { describe, expect, it } from "vitest";
import type { Application, ApplicationStatus } from "@/lib/types";
import { JUST_APPROVED_MS, pipelineView, relDays, statusPill } from "./status";

const ALL: ApplicationStatus[] = ["queued","tailoring","ready_for_review","submitting","submitted","failed","responded","posting_removed"];
const NOW = Date.UTC(2026, 8, 20, 12, 0, 0);
const GREY = /var\(--(?!surface-warm|border-warm)(good|accent|warn|danger|subtle|muted|border|surface|text|label|star|first|offer)[a-z0-9-]*\)/;
const SAGE = "var(--sage)", IDLE = "#f0e3d3", BRICK = "var(--brick)";

function app(status: ApplicationStatus, extra: Partial<Application> = {}): Application {
  return { id: "a1", user_id: "u", job_id: "j", status, master_bullets: [], tailored_bullets: [],
    timeline: [{ at: new Date(NOW - 60 * 60 * 1000).toISOString(), status: "queued" }], ...extra };
}

describe("pipelineView", () => {
  it("always yields four segments", () => {
    for (const s of ALL) expect(pipelineView(app(s), NOW).segments).toHaveLength(4);
  });
  it("fills segments left to right per status", () => {
    const fills = (s: ApplicationStatus, extra?: Partial<Application>) => pipelineView(app(s, extra), NOW).segments.map((x) => x.color);
    expect(fills("queued")).toEqual([SAGE, "var(--terracotta)", IDLE, IDLE]);
    expect(fills("tailoring")).toEqual(fills("queued"));
    expect(fills("ready_for_review")).toEqual([SAGE, SAGE, IDLE, IDLE]);
    expect(fills("submitting")).toEqual([SAGE, SAGE, "var(--terracotta)", IDLE]);
    expect(fills("submitted")).toEqual([SAGE, SAGE, SAGE, IDLE]);
    expect(fills("responded")).toEqual([SAGE, SAGE, SAGE, "var(--honey)"]);
    expect(fills("failed")).toEqual([SAGE, BRICK, IDLE, IDLE]);
    expect(fills("failed", { last_submitted_at: "2026-09-20T00:00:00Z" })).toEqual([SAGE, SAGE, BRICK, IDLE]);
    expect(fills("posting_removed")).toEqual([SAGE, BRICK, IDLE, IDLE]);
  });
  it("pulses only the in-flight segment", () => {
    expect(pipelineView(app("tailoring"), NOW).segments.map((x) => !!x.pulse)).toEqual([false, true, false, false]);
    expect(pipelineView(app("submitting"), NOW).segments.map((x) => !!x.pulse)).toEqual([false, false, true, false]);
    expect(pipelineView(app("submitted"), NOW).segments.some((x) => x.pulse)).toBe(false);
  });
  it("links only the states that need the user", () => {
    for (const s of ALL) expect(!!pipelineView(app(s), NOW).labelHref).toBe(s === "ready_for_review" || s === "failed");
    expect(pipelineView(app("failed"), NOW).labelHref).toBe("/app/applications/a1/review");
  });
  it("tints a just-approved row for two minutes, then not", () => {
    const fresh = app("queued", { timeline: [{ at: new Date(NOW - JUST_APPROVED_MS + 1000).toISOString(), status: "queued" }] });
    expect(pipelineView(fresh, NOW).rightNote?.text).toBe("you said yes ✓");
    expect(pipelineView(fresh, NOW).card?.bg).toBe("#f6faf3");
    expect(pipelineView(app("queued"), NOW).rightNote).toBeUndefined();
    expect(relDays("2026-09-17T12:00:00Z", NOW)).toBe("3d ago");
    expect(pipelineView(app("submitted", { last_submitted_at: "2026-09-17T12:00:00Z" }), NOW).pill?.text).toBe("applied · 3d ago");
  });
  it("never leaks a grey token from either mapper", () => {
    for (const s of ALL) {
      const v = pipelineView(app(s), NOW);
      for (const x of [v.labelColor, ...v.segments.map((q) => q.color), ...Object.values(v.pill ?? {}), ...Object.values(v.card ?? {})]) expect(String(x)).not.toMatch(GREY);
      for (const x of Object.values(statusPill(s))) expect(x).not.toMatch(GREY);
    }
    expect(statusPill("failed").label).toBe("needs you");
    expect(statusPill("ready_for_review").label).toBe("ready for you to check");
  });
});
