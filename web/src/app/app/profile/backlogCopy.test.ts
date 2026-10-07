import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// A source check, not a render: the page imports Firebase and cannot render
// under vitest's node environment.
const page = readFileSync(fileURLToPath(new URL("./page.tsx", import.meta.url)), "utf8");

describe("profile page", () => {
  it("does not render the unscored backlog", () => {
    expect(page).not.toMatch(/unscored_backlog/);
    expect(page).not.toMatch(/not yet scored/);
    expect(page).not.toMatch(/are waiting/);
  });
});
