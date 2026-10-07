import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { ISSUE_LABEL, STATUS_INFO } from "@/lib/adminAccounts";

/** The status and issue values in `tools/account/roster.py` must match the
 *  frontend's label maps, so a new backend value cannot ship unlabelled. */
const PY = readFileSync(
  fileURLToPath(new URL("../../../tools/account/roster.py", import.meta.url)),
  "utf8",
);

function quoted(block: string): string[] {
  return [...block.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

const sorted = (xs: Iterable<string>) => [...new Set(xs)].sort();

describe("roster.py ↔ adminAccounts.ts", () => {
  it("STATUSES and the Status Literal match STATUS_INFO", () => {
    const tuple = PY.match(/^STATUSES = \(([\s\S]*?)\)/m);
    const literal = PY.match(/^Status = Literal\[([\s\S]*?)\]/m);
    expect(tuple, "STATUSES tuple not found").not.toBeNull();
    expect(literal, "Status Literal not found").not.toBeNull();
    const ts = sorted(Object.keys(STATUS_INFO));
    expect(sorted(quoted(tuple![1]))).toEqual(ts);
    expect(sorted(quoted(literal![1]))).toEqual(ts);
  });

  it("every issues.append value matches ISSUE_LABEL", () => {
    const appended = [...PY.matchAll(/issues\.append\("([a-z_]+)"\)/g)].map((m) => m[1]);
    expect(appended.length).toBeGreaterThan(0);
    // An issue added any other way would escape this check.
    expect(PY.match(/issues\.(append|extend|insert)\(/g)?.length).toBe(appended.length);
    expect(sorted(appended)).toEqual(sorted(Object.keys(ISSUE_LABEL)));
  });
});
