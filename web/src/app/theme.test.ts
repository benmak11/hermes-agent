import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const APP = fileURLToPath(new URL(".", import.meta.url));      // web/src/app/
const SRC = join(APP, "..");                                     // web/src/
const css = readFileSync(join(APP, "globals.css"), "utf8");

const GREY = [
  "bg", "surface", "surface-2", "border", "border-mid", "border-strong", "ring", "text", "muted",
  "subtle", "label", "nav-bg", "divider", "accent", "accent-bg", "accent-border", "accent-text",
  "good", "good-bg", "good-border", "warn", "warn-bg", "warn-border", "first-bg", "first-text",
  "danger", "danger-bg", "danger-border", "star", "skeleton", "skeleton-2", "offer-bg",
  "offer-divider", "good-panel-bg", "good-panel-border", "danger-panel-bg", "danger-panel-border",
  "toast-bg", "toast-text", "toast-strong", "toast-btn-bg", "toast-btn-border", "toast-btn-text",
  "toast-kbd-bg", "toast-kbd-border", "toast-kbd-text", "toast-ring-track", "toast-ring", "font-mono",
];
const GREY_USE = new RegExp(`var\\(--(${GREY.join("|")})\\)`);

function sources(): string[] {
  return readdirSync(SRC, { recursive: true })
    .map(String)
    .filter((f) => /\.(tsx?|css)$/.test(f) && !/\.test\.tsx?$/.test(f))
    .map((f) => join(SRC, f));
}

describe("globals.css after facelift PR 8", () => {
  it("has no dark-mode block", () => {
    expect(css).not.toMatch(/prefers-color-scheme/);
  });
  it("declares none of the grey tokens", () => {
    for (const t of GREY) expect(css).not.toMatch(new RegExp(`^\\s*--${t}:`, "m"));
  });
  it("still declares the warm ramp, light-only, on Jakarta", () => {
    for (const t of ["sand", "cream", "surface-warm", "border-warm-hair", "ink", "terracotta", "sage", "honey", "brick"])
      expect(css).toMatch(new RegExp(`^\\s*--${t}:`, "m"));
    expect(css).toMatch(/color-scheme:\s*light/);
    expect(css).toMatch(/--font-jakarta/);
    expect(css).not.toMatch(/geist/i);
  });
});

describe("web/src after facelift PR 8", () => {
  it("nothing reads a grey token, Geist, or the deleted editable module", () => {
    const offenders: string[] = [];
    for (const f of sources()) {
      const s = readFileSync(f, "utf8");
      if (GREY_USE.test(s) || /font-mono|bg-neutral|text-neutral|font-geist|components\/editable"/.test(s))
        offenders.push(f.slice(SRC.length));
    }
    expect(offenders).toEqual([]);
  });
});
