import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/** The review page's wiring, asserted against its source (it imports Firebase
 *  and cannot render in this node-only suite). */
const SRC = readFileSync(fileURLToPath(new URL("./page.tsx", import.meta.url)), "utf8");
const CODE = SRC.replace(/\/\/[^\n]*/g, "").replace(/\/\*[\s\S]*?\*\//g, "");

describe("review page", () => {
  it("never renders a gs:// URI or the raw résumé path", () => {
    expect(CODE).not.toContain("gs://");
    expect(CODE).not.toMatch(/\{\s*app\.resume_variant_uri\s*\}/);
  });

  it("never renders String(error) or a raw failure note", () => {
    expect(CODE).not.toMatch(/String\(\s*(error|err|e)\s*\)/);
    expect(CODE).not.toMatch(/\.message\s*\}/);
    expect(CODE).not.toContain("lastFailed");
    expect(CODE).toContain("{loadErrorMessage(error)}");
  });

  it("marks applied through the mark-applied route and refreshes on success", () => {
    expect(CODE).toContain("`/applications/${id}/mark-applied`, { method: \"POST\" }");
    expect(CODE).toMatch(/markApplied = useMutation\(\{[\s\S]*?onSuccess:\s*invalidateAll,[\s\S]*?onError:\s*\(err\)\s*=>\s*setMarkError\(markAppliedErrorMessage\(err\)\)/);
    expect(CODE).toContain("Mark this as applied?");
  });

  it("gates Submit and the manual path on reviewActions", () => {
    expect(CODE).toContain("reviewActions(app, { submitUnavailable })");
    expect(CODE).toContain("{actions.showSubmit && (");
    expect(CODE).toContain("{actions.showManual && (");
    expect(CODE).toContain("{actions.objectiveEditable ? (");
    // No status-based gate on Submit survives beside the module.
    expect(CODE).not.toMatch(/disabled=\{\s*!\(app\.status === "ready_for_review"/);
    expect(CODE).toMatch(/onError:\s*\(err\)\s*=>\s*\{\s*if \(isSubmitUnavailable\(err\)\) setSubmitUnavailable\(true\);/);
  });

  it("handles objective save errors and refreshes after a save", () => {
    expect(CODE).toMatch(/saveObjective = useMutation\(\{[\s\S]*?onSuccess:\s*invalidateAll,[\s\S]*?onError:\s*\(err\)\s*=>\s*setObjectiveError\(objectiveErrorMessage\(err\)\)/);
  });

  it("opens the posting in a new tab without an opener", () => {
    expect(CODE).toMatch(/target="_blank"\s+rel="noopener noreferrer"\s+className="wm-cta/);
  });

  it("guards the clipboard write", () => {
    expect(CODE).toMatch(/try \{\s*await navigator\.clipboard\.writeText\(objective\);[\s\S]*?\} catch \{\s*setCopied\("failed"\)/);
  });
});
