import { describe, expect, it } from "vitest";
import { barColor, barColorWarm, recPill, recPillWarm, scoreColor, scoreColorWarm } from "@/lib/ui";

const GREY = /var\(--(good|accent|warn|subtle|muted|border|surface|text|label)[a-z-]*\)/;

describe("warm match helpers", () => {
  it("scoreColorWarm maps each recommendation to a warm token", () => {
    expect(scoreColorWarm("strong_apply")).toBe("var(--sage)");
    expect(scoreColorWarm("apply")).toBe("var(--terracotta)");
    expect(scoreColorWarm("maybe")).toBe("var(--honey)");
    expect(scoreColorWarm("skip")).toBe("var(--ink-4)");
  });
  it("recPillWarm returns a Pill tone + the design's label", () => {
    expect(recPillWarm("strong_apply")).toEqual({ tone: "good", label: "STRONG MATCH" });
    expect(recPillWarm("apply")).toEqual({ tone: "accent", label: "GOOD MATCH" });
    expect(recPillWarm("maybe")).toEqual({ tone: "warn", label: "MAYBE" });
    expect(recPillWarm("anything-else")).toEqual({ tone: "muted", label: "WEAK MATCH" });
  });
  it("barColorWarm thresholds match barColor's (80 / 60)", () => {
    expect(barColorWarm(80)).toBe("var(--sage)");
    expect(barColorWarm(79)).toBe("var(--terracotta)");
    expect(barColorWarm(60)).toBe("var(--terracotta)");
    expect(barColorWarm(59)).toBe("var(--honey)");
  });
  it("never leaks a grey token", () => {
    for (const r of ["strong_apply", "apply", "maybe", "skip"]) expect(scoreColorWarm(r)).not.toMatch(GREY);
    for (const v of [0, 60, 80, 100]) expect(barColorWarm(v)).not.toMatch(GREY);
  });
  it("leaves the grey trio for /app/tracking untouched", () => {
    expect(scoreColor("strong_apply")).toBe("var(--good)");
    expect(recPill("apply").label).toBe("APPLY");
    expect(barColor(80)).toBe("var(--good)");
  });
});
