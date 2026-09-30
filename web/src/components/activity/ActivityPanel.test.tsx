import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { ActivityPanel } from "@/components/activity/ActivityPanel";
import { ActivityRow } from "@/components/activity/ActivityRow";
import type {
  ActivityItem,
  ActivityResponse,
  ActivityState,
} from "@/lib/activity";

/**
 * The activity panel renders `GET /activity` and claims nothing beyond it.
 *
 * It replaces `DiscoveryPill` and `ScoringCard`, which both asserted that work
 * was underway on the strength of a `localStorage` flag. So the tests here are
 * about the *negatives*: which rows may move, and when a bar may exist.
 *
 * Node environment, `react-dom/server` — the same shape as `JourneyTrack`'s
 * spec. There is no DOM, so "animates" is asserted as "carries a class that
 * `globals.css` actually animates". The "the animated classes are real" block
 * pins the second half of that: a class with no rule behind it would make
 * every other assertion in this file vacuous.
 */

const CSS = readFileSync(
  fileURLToPath(new URL("../../app/globals.css", import.meta.url)),
  "utf8",
);

const NO_PREF = "@media (prefers-reduced-motion: no-preference)";

/** The stylesheet with every no-preference block cut out, braces matched. */
function withoutNoPreference(css: string): string {
  let out = "";
  let i = 0;
  for (;;) {
    const at = css.indexOf(NO_PREF, i);
    if (at === -1) return out + css.slice(i);
    out += css.slice(i, at);
    let depth = 0;
    let j = css.indexOf("{", at);
    for (; j < css.length; j++) {
      if (css[j] === "{") depth++;
      else if (css[j] === "}" && --depth === 0) break;
    }
    i = j + 1;
  }
}

/** Every class this component uses to move something. */
const ANIMATED = ["wm-act-shimmer", "wm-act-spin", "wm-act-bar"];

const NOW = Date.parse("2026-09-27T12:30:00Z");

function item(state: ActivityState, over: Partial<ActivityItem> = {}): ActivityItem {
  return {
    kind: "scoring",
    state,
    since: "2026-09-27T12:29:00Z",
    next_at: "2026-09-27T13:00:00Z",
    ref: null,
    done: null,
    total: null,
    detail: {},
    ...over,
  };
}

function row(it: ActivityItem): string {
  return renderToStaticMarkup(<ActivityRow item={it} now={NOW} />);
}

function moves(html: string): boolean {
  return ANIMATED.some((c) => html.includes(c));
}

const NINE: ActivityState[] = [
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

describe("the animated classes are real", () => {
  it("each one has an animation rule behind it", () => {
    for (const cls of ANIMATED) {
      // The declaration block that follows the class inside the
      // no-preference query — not merely the class name appearing somewhere.
      const rule = new RegExp(`\\.${cls}\\s*\\{[^}]*animation:`);
      expect(rule.test(CSS), `${cls} has no animation`).toBe(true);
    }
  });

  it("every animation is reduced-motion guarded and falls back to something", () => {
    const still = withoutNoPreference(CSS);
    for (const cls of ANIMATED) {
      const animates = new RegExp(`\\.${cls}\\s*\\{[^}]*animation:`);
      // Nothing moves when motion is off...
      expect(animates.test(still), `${cls} animates unguarded`).toBe(false);
      // ...and the class still paints something, so the row stays legible.
      expect(still.includes(`.${cls} {`), `${cls} has no static form`).toBe(true);
    }
  });
});

describe("ActivityRow — all nine states", () => {
  it("renders every one of them", () => {
    for (const state of NINE) {
      const html = row(item(state));
      expect(html, state).toContain(`data-state="${state}"`);
      // Something legible, not an empty div.
      expect(html.replace(/<[^>]*>/g, "").trim().length, state).toBeGreaterThan(0);
    }
  });

  it("moves on exactly running and queued — nothing else", () => {
    const moving = NINE.filter((s) => moves(row(item(s))));
    expect(moving).toEqual(["queued", "running"]);
    // Including the measured variant of running: a bar is still motion, and
    // still only reachable while something is running.
    expect(moves(row(item("running", { done: 46, total: 200 })))).toBe(true);
  });

  it("gives waiting_external, failed and stalled a still marker", () => {
    for (const state of ["waiting_external", "failed", "stalled"] as const) {
      const html = row(item(state));
      expect(html, state).toContain('data-visual="quiet"');
      expect(moves(html), state).toBe(false);
    }
  });

  it("never renders an -ing verb for a state that is not running", () => {
    for (const state of NINE.filter((s) => s !== "running")) {
      expect(row(item(state)), state).not.toContain("scoring your matches");
    }
    expect(row(item("running"))).toContain("scoring your matches");
  });
});

describe("ActivityRow — the bar", () => {
  it("draws one only when done and total are both numbers", () => {
    const measured = row(item("running", { done: 46, total: 200 }));
    expect(measured).toContain('data-visual="bar"');
    expect(measured).toContain('data-bar-pct="23"');
    expect(measured).toContain("width:23%");

    // A numerator alone is a fabricated percentage. Shimmer, no bar.
    const numeratorOnly = row(item("running", { done: 46, total: null }));
    expect(numeratorOnly).toContain('data-visual="shimmer"');
    expect(numeratorOnly).not.toContain("data-bar-pct");

    const denominatorOnly = row(item("running", { done: null, total: 200 }));
    expect(denominatorOnly).not.toContain("data-bar-pct");

    // And no other state may draw one, whatever numbers came with it.
    for (const state of NINE.filter((s) => s !== "running")) {
      expect(row(item(state, { done: 46, total: 200 })), state).not.toContain(
        "data-bar-pct",
      );
    }
  });

  it("refuses a measured 0 of 0 rather than drawing 0%", () => {
    expect(row(item("running", { done: 0, total: 0 }))).not.toContain(
      "data-bar-pct",
    );
  });

  it("invents no denominator for discovery, which emits no board count", () => {
    const html = row(item("running", { kind: "discovery", done: null, total: null }));
    expect(html).toContain('data-visual="shimmer"');
    expect(html).not.toContain(" of ");
    // Shimmer plus elapsed is the whole honest render.
    expect(html).toContain("1m");
  });
});

// --------------------------------------------------------------------------
// The allowance block
// --------------------------------------------------------------------------

function response(over: Partial<ActivityResponse> = {}): ActivityResponse {
  return {
    polled_at: "2026-09-27T12:30:00Z",
    next_tick_at: "2026-09-27T13:00:00Z",
    items: [item("idle_unscheduled", { kind: "discovery" })],
    allowance: {
      searches: {
        used: 9,
        limit: 14,
        remaining: 5,
        resets_at: "2026-10-05T00:00:00+00:00",
      },
      ratings: {
        used: 2,
        limit: 3,
        remaining: 1,
        remaining_cycle: 3,
        resets_at: "2026-09-28T00:00:00+00:00",
      },
    },
    committed: { usd_low: 0, usd_high: 0, runs: 0 },
    ...over,
  };
}

describe("ActivityPanel — the allowance", () => {
  const html = renderToStaticMarkup(
    <ActivityPanel data={response()} now={NOW} />,
  );

  it("counts searches by the week and ratings by the day", () => {
    expect(html).toContain("9 of 14 searches this week");
    expect(html).toContain("2 of 3 ratings today");
  });

  it("gives each cap its own reset, because they are different instants", () => {
    // The weekly one is days away and carries a weekday; the nightly one does
    // not. Locale-insensitive: only the shape is asserted.
    expect(html).toMatch(/resets \S+ \d?\d:\d\d/);
    expect(html).toMatch(/resets at \d?\d:\d\d/);
  });

  it("says 'no limit', never '0 left', when a cap is off", () => {
    const off = renderToStaticMarkup(
      <ActivityPanel
        data={response({
          allowance: {
            ...response().allowance,
            searches: {
              used: 9,
              limit: null,
              remaining: null,
              resets_at: "2026-10-05T00:00:00+00:00",
            },
          },
        })}
        now={NOW}
      />,
    );
    expect(off).toContain("9 searches this week");
    expect(off).toContain("no limit");
    expect(off).not.toContain("of null");
    expect(off).not.toContain("0 of");
  });

  it("says discovery is off when the server says idle_unscheduled", () => {
    // The `DiscoveryPill` case, end to end: the flag that used to drive it is
    // irrelevant, and what shows is what the record says.
    const idle = renderToStaticMarkup(
      <ActivityPanel
        data={response({ items: [item("idle_unscheduled", { kind: "discovery" })] })}
        now={NOW}
      />,
    );
    expect(idle.toLowerCase()).not.toContain("discovery running");
    expect(idle).not.toContain("finding new roles");
    expect(idle).toContain("new roles is off");
    expect(moves(idle)).toBe(false);
  });

  it("does not promise a reset that will not refill the ratings", () => {
    // Reviewer's case: the day rolled (0 used of 3) but this search's window
    // is spent, so a request right now would be granted nothing and midnight
    // will not change that.
    const blocked = renderToStaticMarkup(
      <ActivityPanel
        data={response({
          allowance: {
            ...response().allowance,
            ratings: {
              used: 0,
              limit: 3,
              remaining: 0,
              remaining_cycle: 0,
              resets_at: "2026-09-28T00:00:00+00:00",
            },
          },
        })}
        now={NOW}
      />,
    );
    expect(blocked).toContain("0 of 3 ratings today");
    expect(blocked).toContain("a new search frees more");
    // Exactly one reset line remains — the weekly one, for searches.
    expect(blocked.match(/resets/g) ?? []).toHaveLength(1);
  });

  it("renders every leg the server sent, in the order it sent them", () => {
    const many = renderToStaticMarkup(
      <ActivityPanel
        data={response({
          items: [
            item("running", { kind: "discovery" }),
            item("idle_unscheduled", { kind: "sweep" }),
            item("queued", { kind: "tailoring" }),
          ],
        })}
        now={NOW}
      />,
    );
    const kinds = [...many.matchAll(/data-kind="([a-z_]+)"/g)].map((m) => m[1]);
    expect(kinds).toEqual(["discovery", "sweep", "tailoring"]);
  });
});
