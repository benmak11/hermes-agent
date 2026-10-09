import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/** The profile page's plan wiring, asserted against its source (it imports
 *  Firebase and cannot render in this node-only suite). */
const SRC = readFileSync(fileURLToPath(new URL("./page.tsx", import.meta.url)), "utf8");
const CODE = SRC.replace(/\/\/[^\n]*/g, "").replace(/\/\*[\s\S]*?\*\//g, "");

describe("profile page plan wiring", () => {
  it("decides the paid controls from the plan the settings response carries", () => {
    expect(CODE).toContain("const showPaid = showPaidScoring(data.plan);");
  });

  it("hides the paid score button from anyone showPaidScoring rejects", () => {
    // The button lives inside the gate, and appears nowhere else.
    expect(CODE).toMatch(/\{showPaid && \(\s*<div[^]*?Score jobs we found…[^]*?<\/div>\s*\)\}/);
    expect(CODE.match(/Score jobs we found…/g)).toHaveLength(1);
  });

  it("never renders the cost sheet outside the same gate", () => {
    expect(CODE).toContain("{showPaid && confirmation && (");
    expect(CODE.match(/<SpendConfirmSheet\b/g)).toHaveLength(1);
  });

  it("takes its plan-dependent wording from discoveryCardCopy", () => {
    expect(CODE).toContain("{copy.trialNote && (");
    expect(CODE).toContain("{copy.discoveryStatus}");
    expect(CODE).toContain("{copy.sweepStatus}");
    expect(CODE).toContain("{copy.findNote}");
  });

  it("has no developer strings", () => {
    expect(CODE).not.toMatch(/CLI/);
    expect(CODE).not.toMatch(/profiles\/\{/);
  });
});
