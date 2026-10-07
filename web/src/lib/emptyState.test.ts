import { describe, expect, it } from "vitest";
import { emptyQueueState } from "@/lib/emptyState";

describe("emptyQueueState", () => {
  it("picks 'Scoring in progress' for a deep backlog but never prints the count", () => {
    for (const pending of [1, 2, 8935, 1_000_000]) {
      const s = emptyQueueState(pending, 0);
      expect(s.heading).toBe("Scoring in progress");
      expect(s.action).toBeNull();
      expect(s.body).not.toBeNull();
      expect(s.body).not.toMatch(/\d/);
    }
  });

  it("keeps the other states selected exactly as before", () => {
    expect(emptyQueueState(0, 0).heading).toBe("No jobs yet");
    expect(emptyQueueState(0, null).heading).toBe("No jobs yet");
    // Something scored, or counts unknown: the threshold state.
    for (const [p, sc] of [[8935, 3], [null, 0], [5, null], [null, null]] as const) {
      const s = emptyQueueState(p, sc);
      expect(s.heading).toBe("You're all caught up");
      expect(s.body).toBeNull();
      expect(s.action).toBe("lower");
    }
  });
});
