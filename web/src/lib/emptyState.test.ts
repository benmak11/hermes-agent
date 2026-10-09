import { describe, expect, it } from "vitest";

import type { ActivityItem } from "@/lib/activity";
import { emptyQueueState, queueContext, type QueueContext } from "@/lib/emptyState";
import type { PlanView } from "@/lib/types";

const plan = (tier: "trial" | "paid", autoActive: boolean): PlanView => ({
  tier,
  trial_started_at: "2026-10-01T00:00:00+00:00",
  auto_until: tier === "paid" ? null : "2026-10-08T00:00:00+00:00",
  auto_active: autoActive,
  scoring_per_day: tier === "paid" ? 10 : 3,
});

const ctx = (over: Partial<QueueContext> = {}): QueueContext => ({
  searching: false,
  scoring: false,
  plan: null,
  ...over,
});

const item = (kind: ActivityItem["kind"], state: ActivityItem["state"]): ActivityItem => ({
  kind,
  state,
  since: null,
  next_at: null,
  ref: null,
  done: null,
  total: null,
  detail: {},
});

describe("emptyQueueState", () => {
  it("says the first search is running only while one is", () => {
    expect(emptyQueueState(0, 0, ctx({ searching: true })).heading).toBe(
      "Your first search is running",
    );
    expect(emptyQueueState(0, 0, ctx()).heading).toBe("No jobs yet");
  });

  it("promises daily matches only during the trial's first week", () => {
    const week = emptyQueueState(0, 0, ctx({ plan: plan("trial", true) }));
    expect(week.body).toBe(
      "New matches arrive daily during your first week. After that, use Find new jobs on your Profile anytime.",
    );
    for (const p of [plan("trial", false), plan("paid", true), null]) {
      const s = emptyQueueState(0, 0, ctx({ plan: p }));
      expect(s.body).toBe("Use Find new jobs on your Profile to search for new openings.");
    }
  });

  it("never claims a schedule or a place that does not exist", () => {
    for (const p of [plan("trial", true), plan("trial", false), plan("paid", true), null]) {
      const s = emptyQueueState(0, 0, ctx({ plan: p }));
      expect(s.body).not.toMatch(/Companies/);
      expect(s.body).not.toMatch(/on a schedule/);
    }
  });

  it("claims scoring is in progress only while it is, and never prints the count", () => {
    for (const pending of [1, 2, 8935, 1_000_000]) {
      const running = emptyQueueState(pending, 0, ctx({ scoring: true }));
      expect(running.heading).toBe("Scoring in progress");
      expect(running.body).not.toMatch(/\d/);

      for (const p of [plan("trial", true), plan("trial", false), plan("paid", true), null]) {
        const idle = emptyQueueState(pending, 0, ctx({ plan: p }));
        expect(idle.heading).toBe("Jobs found, not yet rated");
        expect(idle.action).toBeNull();
        expect(idle.body).not.toMatch(/\d/);
        expect(idle.body).not.toMatch(/in progress|being scored/);
      }
    }
  });

  it("keeps the threshold state selected exactly as before", () => {
    expect(emptyQueueState(0, null).heading).toBe("No jobs yet");
    for (const [p, sc] of [[8935, 3], [null, 0], [5, null], [null, null]] as const) {
      const s = emptyQueueState(p, sc, ctx({ searching: true, scoring: true }));
      expect(s.heading).toBe("You're all caught up");
      expect(s.body).toBeNull();
      expect(s.action).toBe("lower");
    }
  });
});

describe("queueContext", () => {
  it("reads live discovery and scoring legs off the activity items", () => {
    const c = queueContext(
      [item("discovery", "running"), item("batch_scoring", "waiting_external")],
      null,
    );
    expect(c.searching).toBe(true);
    expect(c.scoring).toBe(true);
  });

  it("does not count idle or finished legs as live", () => {
    const c = queueContext(
      [item("discovery", "idle_scheduled"), item("scoring", "finished")],
      null,
    );
    expect(c.searching).toBe(false);
    expect(c.scoring).toBe(false);
  });
});
