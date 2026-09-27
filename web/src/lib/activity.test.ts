import { describe, expect, it } from "vitest";
import {
  ACTIVITY_LABELS,
  POLL_ACTIVE_MS,
  POLL_EXTERNAL_MS,
  activityView,
  elapsedSince,
  pollMs,
  type ActivityItem,
  type ActivityKind,
  type ActivityState,
} from "./activity";

const NOW = Date.UTC(2026, 8, 27, 12, 30, 0);
const ALL_STATES: ActivityState[] = [
  "never_started",
  "idle_scheduled",
  "idle_unscheduled",
  "queued",
  "running",
  "waiting_external",
  "finished",
  "failed",
  "stalled",
];
const ALL_KINDS: ActivityKind[] = [
  "discovery",
  "sweep",
  "scoring",
  "batch_scoring",
  "extract",
  "tailoring",
  "submission",
];

/**
 * A genuine claim that work is underway right now.
 *
 * Not a blanket `/\w+ing/`: "match scoring" and "posting checks" are gerund
 * *nouns* and are exactly what the non-running states are supposed to say. What
 * must never appear outside `running` is a progressive construction, or one of
 * the `running` branch's own labels.
 */
const PROGRESSIVE = /\b(?:is|are|we(?:'re| are))\s+\w+ing\b|in progress|underway/;
const RUNNING_LABELS = new Set(Object.values(ACTIVITY_LABELS));
const PERCENT = /%|\bof\b|\d+\s*\/\s*\d+/;

function item(
  state: ActivityState,
  extra: Partial<ActivityItem> = {},
): ActivityItem {
  return {
    kind: "scoring",
    state,
    since: new Date(NOW - 90_000).toISOString(),
    next_at: null,
    ref: null,
    done: null,
    total: null,
    detail: {},
    ...extra,
  };
}

describe("activityView — the bar", () => {
  it("draws a bar only when done and total are both numbers", () => {
    expect(activityView(item("running", { done: 2, total: 8 }), NOW).visual).toBe(
      "bar",
    );
    // A numerator with no denominator is a fabricated percentage.
    expect(activityView(item("running", { done: 2, total: null }), NOW).visual).toBe(
      "shimmer",
    );
    expect(activityView(item("running", { done: null, total: 8 }), NOW).visual).toBe(
      "shimmer",
    );
    expect(activityView(item("running"), NOW).visual).toBe("shimmer");
  });

  it("no state other than running can ever reach a bar", () => {
    for (const state of ALL_STATES) {
      if (state === "running") continue;
      // Even handed a complete pair — which the server would not send — a
      // non-running state must not render as measurable progress.
      const view = activityView(item(state, { done: 2, total: 8 }), NOW);
      expect(view.visual, state).not.toBe("bar");
    }
  });

  it("only a bar view quotes a count", () => {
    for (const state of ALL_STATES) {
      const view = activityView(item(state, { done: 2, total: 8 }), NOW);
      if (view.visual === "bar") continue;
      expect(view.verb, state).not.toMatch(PERCENT);
    }
  });
});

describe("activityView — idle must not look busy", () => {
  it("idle_unscheduled gets no spinner and no present-progressive verb", () => {
    for (const kind of ALL_KINDS) {
      const view = activityView(item("idle_unscheduled", { kind }), NOW);
      expect(view.visual, kind).toBe("none");
      expect(view.verb, kind).not.toMatch(PROGRESSIVE);
      expect(RUNNING_LABELS.has(view.verb), kind).toBe(false);
      expect(view.tone, kind).toBe("idle");
      // Nothing is scheduled, so there is no time to advertise…
      expect(view.nextLine, kind).toBe("");
      // …and no elapsed time either: nothing is elapsing.
      expect(view.elapsed, kind).toBe("");
    }
  });

  it("no idle or terminal state gets a spinner or a shimmer", () => {
    for (const state of [
      "never_started",
      "idle_scheduled",
      "idle_unscheduled",
      "finished",
      "failed",
      "stalled",
    ] as ActivityState[]) {
      const visual = activityView(item(state), NOW).visual;
      expect(["none", "quiet"], state).toContain(visual);
    }
  });

  it("a queued item says queued rather than naming the work as underway", () => {
    const view = activityView(item("queued", { kind: "tailoring" }), NOW);
    expect(view.verb).toBe("resume writing is queued");
    expect(view.visual).toBe("spinner");
  });

  it("stalled never reads as running", () => {
    const view = activityView(item("stalled"), NOW);
    expect(view.verb).not.toMatch(PROGRESSIVE);
    expect(RUNNING_LABELS.has(view.verb)).toBe(false);
    expect(view.tone).toBe("problem");
    expect(view.visual).toBe("quiet");
    // The one number it may show is how long it has been stuck — measured.
    expect(view.elapsed).toBe("1m");
  });
});

describe("activityView — waiting_external", () => {
  const waiting = item("waiting_external", {
    kind: "batch_scoring",
    next_at: new Date(Date.UTC(2026, 8, 27, 13, 0, 0)).toISOString(),
    done: 3,
    total: 9,
  });

  it("prints a next-check time and never a percent", () => {
    const view = activityView(waiting, NOW);
    expect(view.nextLine).toMatch(/next check at \d?\d:\d\d/);
    expect(view.verb).not.toMatch(PERCENT);
    expect(view.visual).toBe("quiet");
    expect(view.tone).toBe("waiting");
  });

  it("says who we are waiting on, because it is not us", () => {
    expect(activityView(waiting, NOW).verb).toContain("Google");
  });

  it("shows elapsed but no next line when no schedule was sent", () => {
    const view = activityView({ ...waiting, next_at: null }, NOW);
    expect(view.nextLine).toBe("");
    expect(view.elapsed).toBe("1m");
  });
});

describe("activityView — every state is handled", () => {
  it("returns a well-formed view for all nine states and all seven kinds", () => {
    for (const state of ALL_STATES) {
      for (const kind of ALL_KINDS) {
        const view = activityView(item(state, { kind }), NOW);
        expect(view.verb, `${kind}/${state}`).toBeTruthy();
        expect(
          ["none", "shimmer", "spinner", "bar", "quiet"],
          `${kind}/${state}`,
        ).toContain(view.visual);
      }
    }
  });
});

describe("elapsedSince", () => {
  it("counts up, and refuses to invent a number", () => {
    expect(elapsedSince(new Date(NOW - 4_000).toISOString(), NOW)).toBe("4s");
    expect(elapsedSince(new Date(NOW - 3 * 60_000).toISOString(), NOW)).toBe("3m");
    expect(elapsedSince(new Date(NOW - 130 * 60_000).toISOString(), NOW)).toBe(
      "2h 10m",
    );
    expect(elapsedSince(new Date(NOW - 120 * 60_000).toISOString(), NOW)).toBe("2h");
    expect(elapsedSince(null, NOW)).toBe("");
    expect(elapsedSince("not a date", NOW)).toBe("");
    // A clock skew must not render as a countdown to something.
    expect(elapsedSince(new Date(NOW + 60_000).toISOString(), NOW)).toBe("");
  });
});

describe("pollMs", () => {
  it("polls fast only for a live claim", () => {
    expect(pollMs([item("running")])).toBe(POLL_ACTIVE_MS);
    expect(pollMs([item("queued")])).toBe(POLL_ACTIVE_MS);
  });

  it("polls a Vertex batch at the tick's cadence, not the UI's", () => {
    expect(pollMs([item("waiting_external")])).toBe(POLL_EXTERNAL_MS);
  });

  it("is false for every terminal and idle state", () => {
    for (const state of [
      "never_started",
      "idle_scheduled",
      "idle_unscheduled",
      "finished",
      "failed",
      "stalled",
    ] as ActivityState[]) {
      expect(pollMs([item(state)]), state).toBe(false);
    }
    expect(pollMs([])).toBe(false);
  });

  it("the fastest live item wins across a mixed list", () => {
    expect(
      pollMs([item("idle_unscheduled"), item("waiting_external"), item("running")]),
    ).toBe(POLL_ACTIVE_MS);
    expect(pollMs([item("finished"), item("waiting_external")])).toBe(
      POLL_EXTERNAL_MS,
    );
    expect(pollMs([item("finished"), item("idle_scheduled")])).toBe(false);
  });
});
