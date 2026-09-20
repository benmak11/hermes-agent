import { describe, expect, it } from "vitest";
import { copy } from "@/components/marketing/copy";

function strings(node: unknown, out: string[] = []): string[] {
  if (typeof node === "string") out.push(node);
  else if (Array.isArray(node)) node.forEach((n) => strings(n, out));
  else if (node && typeof node === "object")
    Object.values(node).forEach((n) => strings(n, out));
  return out;
}

describe("marketing copy", () => {
  const all = strings(copy);

  it("carries none of the design's retired phrasings", () => {
    for (const retired of [
      "Get started free",
      "Export everything",
      "paste their email",
      "No credit card",
    ]) {
      const hits = all.filter((s) => s.includes(retired));
      expect(hits, retired).toEqual([]);
    }
  });

  it("uses the same CTA text in all four placements", () => {
    // Deliberately four keys, not one: the sharpening pass is per placement,
    // and this is the conscious checkpoint if they ever diverge.
    expect(copy.hero.cta).toBe(copy.nav.cta);
    expect(copy.closing.cta).toBe(copy.nav.cta);
    expect(copy.footer.start.cta).toBe(copy.nav.cta);
  });
});
