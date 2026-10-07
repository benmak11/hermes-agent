import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/** The Admin link, asserted against TopNav's source: the component needs
 *  Firebase and react-query, and this suite has no DOM. */
const SRC = readFileSync(fileURLToPath(new URL("./TopNav.tsx", import.meta.url)), "utf8");
const CODE = SRC.replace(/\/\/[^\n]*/g, "").replace(/\/\*[\s\S]*?\*\//g, "");

describe("TopNav's Admin link", () => {
  it("is not in the static LINKS array everyone sees", () => {
    const links = CODE.match(/const LINKS[^=]*=\s*\[([\s\S]*?)\];/);
    expect(links).not.toBeNull();
    expect(links![1]).not.toMatch(/admin/i);
  });

  it("asks /admin/me, keyed per user, once per session, without retrying", () => {
    expect(CODE).toMatch(/queryKey:\s*\["admin",\s*"me",\s*user\?\.uid\]/);
    expect(CODE).toContain('apiFetch<{ admin: boolean }>("/admin/me")');
    expect(CODE).toMatch(/enabled:\s*!!user/);
    expect(CODE).toMatch(/staleTime:\s*Infinity/);
    expect(CODE).toMatch(/retry:\s*false/);
  });

  it("renders only when /admin/me answers admin: true", () => {
    const hrefs = [...CODE.matchAll(/href="\/app\/admin"/g)];
    expect(hrefs).toHaveLength(1);
    const before = CODE.slice(0, hrefs[0].index);
    // The nearest JSX condition opening before the link is the admin answer.
    const cond = before.slice(before.lastIndexOf("{"));
    expect(cond).toMatch(/^\{adminMe\?\.admin === true && \(\s*<Link\s*$/);
  });

  it("has a section label", () => {
    expect(CODE).toMatch(/\|\s*"admin"/);
    expect(CODE).toMatch(/admin:\s*"Admin"/);
  });
});
