import { describe, expect, it } from "vitest";

import { discoveryCardCopy, showPaidScoring } from "@/lib/discoveryCard";
import type { DiscoverySettings, PlanView } from "@/lib/types";

const NOW = Date.parse("2026-10-09T12:00:00Z");
const DAY = 24 * 60 * 60 * 1000;
const iso = (ms: number) => new Date(ms).toISOString();

const ON: DiscoverySettings = {
  auto_discovery: true,
  discovery_interval_hours: 24,
  liveness_sweep: true,
  sweep_interval_hours: 24,
};
const OFF: DiscoverySettings = { ...ON, auto_discovery: false, liveness_sweep: false };

const trial = (daysLeft: number): PlanView => ({
  tier: "trial",
  trial_started_at: iso(NOW - (7 - daysLeft) * DAY),
  auto_until: iso(NOW + daysLeft * DAY),
  auto_active: daysLeft > 0,
  scoring_per_day: 3,
});
const PAID: PlanView = {
  tier: "paid",
  trial_started_at: iso(NOW - 40 * DAY),
  auto_until: null,
  auto_active: true,
  scoring_per_day: 10,
};

const copy = (plan: PlanView | undefined, settings = ON) =>
  discoveryCardCopy({
    plan,
    settings,
    lastDiscoveryAt: iso(NOW - 3 * 60 * 60 * 1000),
    nextDiscoveryAt: iso(NOW + 21 * 60 * 60 * 1000),
    lastSweepAt: iso(NOW - 3 * 60 * 60 * 1000),
    nextSweepAt: iso(NOW + 21 * 60 * 60 * 1000),
    now: NOW,
  });

describe("showPaidScoring", () => {
  it("shows the paid button only on an explicit paid plan", () => {
    expect(showPaidScoring(PAID)).toBe(true);
    expect(showPaidScoring(trial(5))).toBe(false);
    expect(showPaidScoring(trial(-1))).toBe(false);
    expect(showPaidScoring(undefined)).toBe(false);
    expect(showPaidScoring(null)).toBe(false);
  });
});

describe("discoveryCardCopy", () => {
  it("tells a trial in its first week how long daily search lasts", () => {
    const c = copy(trial(5));
    expect(c.trialNote).toBe("Daily search is included in your first week — 5 days left.");
    expect(c.discoveryStatus).toBe("last 3h ago · next in 21h");
    expect(c.sweepStatus).toBe("last 3h ago · next in 21h");
    expect(copy(trial(0.2)).trialNote).toMatch(/— 1 day left\.$/);
  });

  it("owns up when the free week is over and promises no next run", () => {
    const c = copy(trial(-1));
    expect(c.trialNote).toBe(
      "Your free week of daily searches has ended — use Find new jobs anytime.",
    );
    expect(c.discoveryStatus).toBe("last 3h ago · paused after your first week");
    expect(c.sweepStatus).toBe("last 3h ago · paused after your first week");
    expect(c.discoveryStatus).not.toMatch(/next/);
  });

  it("says nothing about a trial on a paid plan", () => {
    const c = copy(PAID);
    expect(c.trialNote).toBeNull();
    expect(c.discoveryStatus).toBe("last 3h ago · next in 21h");
    expect(c.findNote).toMatch(/costs money/);
  });

  it("tells a trial that Find new jobs scores, free, up to the daily cap", () => {
    expect(copy(trial(5)).findNote).toBe(
      "Find new jobs also scores up to 3 new matches a day, free during your trial.",
    );
    expect(copy(trial(5)).findNote).not.toMatch(/costs money/);
  });

  it("has no developer strings when the loops are off", () => {
    for (const plan of [trial(5), trial(-1), PAID, undefined]) {
      const c = copy(plan, OFF);
      expect(c.discoveryStatus).toBe("off — use Find new jobs below");
      expect(c.discoveryStatus).not.toMatch(/CLI/);
      expect(c.sweepStatus).toBe("off — taken-down postings stay until acted on");
    }
  });

  it("reads like before when an older API sends no plan", () => {
    const c = copy(undefined);
    expect(c.trialNote).toBeNull();
    expect(c.discoveryStatus).toBe("last 3h ago · next in 21h");
  });
});
