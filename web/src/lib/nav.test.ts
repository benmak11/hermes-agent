import { describe, expect, it } from "vitest";
import { loginHref, safeNext, signupFrom } from "@/lib/nav";

describe("safeNext", () => {
  it("accepts /app itself and paths under it, query included", () => {
    expect(safeNext("/app")).toBe("/app");
    expect(safeNext("/app/tracking?tab=starred")).toBe("/app/tracking?tab=starred");
    expect(safeNext("/app?x=1")).toBe("/app?x=1");
  });

  it("rejects a path that merely starts with the letters 'app'", () => {
    expect(safeNext("/apple")).toBeNull();
  });

  it("rejects non-app paths and empty input", () => {
    expect(safeNext("/login")).toBeNull();
    expect(safeNext("/")).toBeNull();
    expect(safeNext(null)).toBeNull();
    expect(safeNext("")).toBeNull();
  });

  it("rejects absolute URLs and schemes", () => {
    expect(safeNext("https://evil.example/app")).toBeNull();
    expect(safeNext("javascript:alert(1)")).toBeNull();
  });

  it("rejects protocol-relative and double-slash paths", () => {
    expect(safeNext("//evil.example")).toBeNull();
    expect(safeNext("/app//evil.example")).toBeNull();
  });

  it("rejects backslashes", () => {
    expect(safeNext("/app\\evil")).toBeNull();
    // Past the `tail` check, so only the backslash clause itself catches this one.
    expect(safeNext("/app/x\\evil")).toBeNull();
  });

  it("rejects dot-dot traversal", () => {
    expect(safeNext("/app/../login")).toBeNull();
  });

  it("rejects percent-encoded dot segments that resolve outside /app", () => {
    // The literal ".." check never sees these; only URL normalisation does.
    expect(safeNext("/app/%2e%2e/login")).toBeNull();
    expect(safeNext("/app/.%2e/onboarding")).toBeNull();
    expect(safeNext("/app/%2e%2e/%2e%2e/")).toBeNull();
    // Encoded segments that stay under /app are fine.
    expect(safeNext("/app/%2e/tracking")).toBe("/app/%2e/tracking");
  });

  it("rejects CR/LF", () => {
    expect(safeNext("/app\r\nX: y")).toBeNull();
    // Past the `tail` check, so only the CR/LF clause itself catches this one.
    expect(safeNext("/app/x\r\nX: y")).toBeNull();
  });
});

describe("signupFrom", () => {
  it("returns each known source as itself", () => {
    expect(signupFrom("hero")).toBe("hero");
    expect(signupFrom("closing")).toBe("closing");
    expect(signupFrom("nav")).toBe("nav");
    expect(signupFrom("footer")).toBe("footer");
  });

  it("returns null for anything else, case-sensitively", () => {
    expect(signupFrom("x")).toBeNull();
    expect(signupFrom("HERO")).toBeNull();
    expect(signupFrom("")).toBeNull();
    expect(signupFrom(null)).toBeNull();
  });
});

describe("loginHref", () => {
  it("encodes pathname plus search into ?next=", () => {
    expect(loginHref("/app/tracking", "?tab=starred&x=1")).toBe(
      "/login?next=%2Fapp%2Ftracking%3Ftab%3Dstarred%26x%3D1",
    );
  });

  it("round-trips through safeNext with the full query intact", () => {
    // The `&x=1` is load-bearing: unencoded, it would split off as its own
    // param on /login and the assertion below would lose it.
    const href = loginHref("/app/tracking", "?tab=starred&x=1");
    const next = new URL(href, "http://h").searchParams.get("next");
    expect(safeNext(next)).toBe("/app/tracking?tab=starred&x=1");
  });
});
