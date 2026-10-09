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

  it("grants and revokes through the two seat routes and refetches on success", () => {
    expect(CODE).toContain('apiFetch<GrantResponse>("/admin/seats", {');
    expect(CODE).toContain('apiFetch<RevokeResponse>("/admin/seats/revoke", {');
    expect(CODE).toMatch(/invalidateQueries\(\{\s*queryKey:\s*\["admin",\s*"accounts"\]\s*\}\)/);
    expect((CODE.match(/onSuccess:\s*\(res\)\s*=>\s*done\(/g) ?? []).length).toBe(2);
    // The revoke sends what was typed, so the server checks it too.
    expect(CODE).toContain("confirm: panel.typed");
  });
});

describe("/app/admin board health", () => {
  it("fetches /admin/boards only once the accounts call shows the admin", () => {
    expect(CODE).toMatch(/queryKey:\s*\["admin",\s*"boards"\]/);
    expect(CODE).toContain('apiFetch<BoardHealth>("/admin/boards")');
    expect(CODE).toMatch(/enabled:\s*!!user\s*&&\s*showsAdminNav\(view\)/);
    expect((CODE.match(/retry:\s*false/g) ?? []).length).toBe(2);
  });

  it("decides its state with boardsView and renders only beside the admin view", () => {
    expect(CODE).toContain("boardsView(");
    expect(CODE).toContain("{showsAdminNav(view) && <BoardHealthSection view={boardView} />}");
    expect(CODE).toContain("{NO_BOARDS}");
  });

  it("is view-only and marks failing rows in brick", () => {
    expect(CODE).not.toMatch(/apiFetch<[^>]*>\("\/admin\/boards",/);
    expect(CODE).toMatch(/isFailing\(row\)\s*\?\s*"var\(--brick\)"/);
  });
});
