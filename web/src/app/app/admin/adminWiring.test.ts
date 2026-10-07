import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/** How `/app/admin` fetches, asserted against its source (a client page over
 *  Firebase and react-query cannot render in this node-only suite). */
const SRC = readFileSync(fileURLToPath(new URL("./page.tsx", import.meta.url)), "utf8");
const CODE = SRC.replace(/\/\/[^\n]*/g, "").replace(/\/\*[\s\S]*?\*\//g, "");

describe("/app/admin data fetching", () => {
  it("fetches /admin/accounts once the user is known", () => {
    expect(CODE).toMatch(/queryKey:\s*\["admin",\s*"accounts"\]/);
    expect(CODE).toContain('apiFetch<Roster>("/admin/accounts")');
    expect(CODE).toMatch(/enabled:\s*!!user/);
  });

  it("does not retry a 404 and keeps no roster in the cache after unmount", () => {
    expect(CODE).toMatch(/retry:\s*false/);
    expect(CODE).toMatch(/gcTime:\s*0\b/);
  });

  it("decides its state with adminView and does not use notFound()", () => {
    expect(CODE).toContain("adminView(");
    expect(CODE).toContain('{showsAdminNav(view) && <TopNav section="admin" />}');
    expect(CODE).not.toMatch(/notFound\s*\(|forbidden\s*\(/);
  });
});
