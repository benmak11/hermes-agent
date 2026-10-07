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

const TS = readFileSync(
  fileURLToPath(new URL("./adminAccounts.ts", import.meta.url)),
  "utf8",
);
const ROUTES = readFileSync(
  fileURLToPath(new URL("../../../api/routes/admin.py", import.meta.url)),
  "utf8",
);

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

  it("the Summary model's fields match the Summary type", () => {
    const py = PY.match(/^class Summary\(BaseModel\):\n([\s\S]*?)\n\n/m);
    const ts = TS.match(/^export type Summary = \{([\s\S]*?)\};/m);
    expect(py, "class Summary not found").not.toBeNull();
    expect(ts, "type Summary not found").not.toBeNull();
    const pyFields = [...py![1].matchAll(/^ {4}([a-z_]+):/gm)].map((m) => m[1]);
    const tsFields = [...ts![1].matchAll(/^ {2}([a-z_]+):/gm)].map((m) => m[1]);
    expect(pyFields).toContain("seat_cap");
    expect(sorted(tsFields)).toEqual(sorted(pyFields));
  });

  it("the not-configured detail seatError looks for is the one admin.py sends", () => {
    expect(TS).toContain('detail.startsWith("seat cap not configured")');
    expect(ROUTES).toContain('"seat cap not configured: ');
  });
});
