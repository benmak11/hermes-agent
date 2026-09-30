import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * How `/app` is wired to `/activity` — asserted against the page's source.
 *
 * The page cannot be rendered here: it is a client component over
 * `@tanstack/react-query`, Firebase auth and `next/navigation`, and this
 * suite is deliberately node-only with no DOM (`vitest.config.mts`). So the
 * two properties that matter are pinned the way `app/theme.test.ts` already
 * pins its cross-cutting rule — by reading the file.
 *
 * Both are *negatives*, and both are regressions this repo actually shipped:
 *
 * 1. **No claim that work is happening comes from `localStorage`.**
 *    `DiscoveryPill` rendered "discovery running" with a spinner whenever the
 *    `hermes:firstRun` flag was set — for five minutes after onboarding,
 *    whether or not discovery was running, and with no way of knowing.
 *    `ScoringCard` did the same with "scoring…". Both are deleted; the panel
 *    renders the server's own answer instead.
 * 2. **The poll cadence is `pollMs`, not a constant.** `firstRun ? 5000 :
 *    30000` polled a steady-state screen forever and polled a dead one hard.
 */

const SRC_DIR = fileURLToPath(new URL("../..", import.meta.url));

const SRC = readFileSync(
  fileURLToPath(new URL("./page.tsx", import.meta.url)),
  "utf8",
);

describe("/app no longer claims work from a localStorage flag", () => {
  it("has no DiscoveryPill and no 'discovery running' string", () => {
    expect(SRC).not.toMatch(/DiscoveryPill/);
    expect(SRC.toLowerCase()).not.toContain("discovery running");
  });

  it("has no ScoringCard and no 'scoring…' placeholder", () => {
    expect(SRC).not.toMatch(/ScoringCard/);
    expect(SRC).not.toContain("scoring…");
  });

  it("reads no first-run flag at all", () => {
    // Only the comment explaining what was removed may mention it.
    const code = SRC.replace(/\/\/[^\n]*/g, "").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(code).not.toMatch(/firstRun/);
    expect(code).not.toMatch(/useFirstRun|clearFirstRun/);
  });

  it("renders the panel from the server's response", () => {
    expect(SRC).toContain("<ActivityPanel");
    expect(SRC).toContain('apiFetch<ActivityResponse>("/activity")');
  });
});

describe("/app polls at pollMs(items)", () => {
  it("uses pollMs for both queries and no hard-coded interval", () => {
    expect(SRC).toContain("pollMs(");
    const intervals = [...SRC.matchAll(/refetchInterval:\s*([^,\n]+)/g)].map(
      (m) => m[1].trim(),
    );
    expect(intervals.length).toBeGreaterThan(0);
    for (const value of intervals) {
      expect(value, value).toMatch(/pollMs|poll$/);
      // A literal millisecond count is exactly what this replaces.
      expect(value, value).not.toMatch(/\d{3,}/);
    }
  });
});

describe("the first-run flag is gone from the tree, not just from /app", () => {
  it("nothing writes or reads hermes:firstRun any more", () => {
    // Deleting a component but leaving the flag being written is how dead
    // plumbing comes back: the next person finds a live flag and a reader is
    // one line away. Exported dead code is invisible to eslint, so it is
    // asserted here instead.
    const offenders = readdirSync(SRC_DIR, { recursive: true })
      .map(String)
      .filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
      .filter((f) => {
        // Comments stripped: a note explaining what was deleted is not a
        // reintroduction of it.
        const code = readFileSync(join(SRC_DIR, f), "utf8")
          .replace(/\/\/[^\n]*/g, "")
          .replace(/\/\*[\s\S]*?\*\//g, "");
        return /hermes:firstRun|markFirstRun|useFirstRun|clearFirstRun/.test(code);
      });
    expect(offenders).toEqual([]);
  });
});
