import { describe, expect, it } from "vitest";
import { HERO_STAGES } from "@/components/marketing/heroDemo";

describe("hero demo track", () => {
  it("is the design's five stages in order", () => {
    expect(HERO_STAGES.map((s) => s.name)).toEqual([
      "Applied",
      "Recruiter call",
      "System design",
      "Hiring manager",
      "Decision",
    ]);
  });

  it("has exactly one current stage, the third", () => {
    const current = HERO_STAGES.map((s, i) => (s.status === "current" ? i : -1)).filter(
      (i) => i >= 0,
    );
    expect(current).toEqual([2]);
  });

  it("tones each note like the design", () => {
    expect(HERO_STAGES.map((s) => s.noteTone)).toEqual([
      "muted",
      "good",
      "accent",
      "muted",
      "muted",
    ]);
  });
});
