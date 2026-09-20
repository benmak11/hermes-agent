import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CompanyTile } from "@/components/warm/CompanyTile";
import { MatchChip } from "@/components/warm/MatchChip";
import { Pill } from "@/components/warm/Pill";

describe("Pill", () => {
  it("maps each tone to its warm colour pair", () => {
    const good = renderToStaticMarkup(<Pill tone="good">ok</Pill>);
    expect(good).toContain("color:var(--sage)");
    expect(good).toContain("background:var(--sage-tint)");
    expect(good).toContain(">ok<");

    const accent = renderToStaticMarkup(<Pill tone="accent">a</Pill>);
    expect(accent).toContain("color:var(--terracotta)");
    expect(accent).toContain("border:1px solid var(--border-warm)");

    const warn = renderToStaticMarkup(<Pill tone="warn">w</Pill>);
    expect(warn).toContain("color:#9a6216");
    expect(warn).toContain("background:var(--honey-tint)");

    const muted = renderToStaticMarkup(<Pill tone="muted">m</Pill>);
    expect(muted).toContain("color:var(--ink-4)");
    expect(muted).toContain("border:1px solid var(--border-warm-hair)");
  });
});

describe("CompanyTile", () => {
  it("maps each hue to its background/foreground pair", () => {
    const pairs = {
      violet: ["#f3edf7", "#7a5a94"],
      honey: ["var(--honey-tint)", "#9a6216"],
      sage: ["var(--sage-tint)", "var(--sage)"],
      terracotta: ["var(--terracotta-tint)", "var(--terracotta)"],
    } as const;
    for (const [hue, [bg, fg]] of Object.entries(pairs)) {
      const html = renderToStaticMarkup(
        <CompanyTile initial="S" hue={hue as keyof typeof pairs} />,
      );
      expect(html).toContain(`background:${bg}`);
      expect(html).toContain(`color:${fg}`);
      expect(html).toContain(">S<");
    }
  });
});

describe("MatchChip", () => {
  it("renders the label in sage on sage-tint", () => {
    const html = renderToStaticMarkup(<MatchChip label="92% match" />);
    expect(html).toContain("color:var(--sage)");
    expect(html).toContain("background:var(--sage-tint)");
    expect(html).toContain("92% match");
  });
});
