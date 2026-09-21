import { describe, expect, it } from "vitest";
import { barColor, recPill, scoreColor } from "@/lib/ui";

const GREY = /var\(--(good|accent|warn|subtle|muted|border|surface|text|label)[a-z-]*\)/;

describe("warm match helpers", () => {
  it("scoreColor maps each recommendation to a warm token", () => {
    expect(scoreColor("strong_apply")).toBe("var(--sage)");
    expect(scoreColor("apply")).toBe("var(--terracotta)");
    expect(scoreColor("maybe")).toBe("var(--honey)");
    expect(scoreColor("skip")).toBe("var(--ink-4)");
  });
  it("recPill returns a Pill tone + the design's label", () => {
    expect(recPill("strong_apply")).toEqual({ tone: "good", label: "STRONG MATCH" });
    expect(recPill("apply")).toEqual({ tone: "accent", label: "GOOD MATCH" });
    expect(recPill("maybe")).toEqual({ tone: "warn", label: "MAYBE" });
    expect(recPill("anything-else")).toEqual({ tone: "muted", label: "WEAK MATCH" });
  });
  it("barColor thresholds are 80 / 60", () => {
    expect(barColor(80)).toBe("var(--sage)");
    expect(barColor(79)).toBe("var(--terracotta)");
    expect(barColor(60)).toBe("var(--terracotta)");
    expect(barColor(59)).toBe("var(--honey)");
  });
  it("never leaks a grey token", () => {
    for (const r of ["strong_apply", "apply", "maybe", "skip"]) expect(scoreColor(r)).not.toMatch(GREY);
    for (const v of [0, 60, 80, 100]) expect(barColor(v)).not.toMatch(GREY);
  });
});
