import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/** A source check: the page imports Firebase and cannot render here. */
const SRC = readFileSync(fileURLToPath(new URL("./page.tsx", import.meta.url)), "utf8");
const CODE = SRC.replace(/\/\/[^\n]*/g, "").replace(/\/\*[\s\S]*?\*\//g, "");

describe("onboarding review page", () => {
  it("never shows where the profile is stored", () => {
    expect(CODE).not.toMatch(/profiles\//);
    expect(CODE).not.toMatch(/\{uid\}/);
  });

  it("still reports the edits the user made", () => {
    expect(CODE).toContain("{editNote(fieldsCorrected, skillsRemoved)}");
  });
});
