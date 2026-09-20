import { describe, expect, it } from "vitest";
import { signupOutcome } from "@/lib/signupFlow";

describe("signupOutcome", () => {
  it("continues when the backend says allowed", () => {
    expect(signupOutcome({ kind: "ok", allowed: true })).toBe("continue");
  });

  it("waitlists when the backend says not allowed", () => {
    expect(signupOutcome({ kind: "ok", allowed: false })).toBe("waitlist");
  });

  it("falls back to the legacy probe when the route is not deployed yet (404)", () => {
    expect(signupOutcome({ kind: "error", status: 404 })).toBe("fallback");
  });

  it("locks out on 403 — the token has no email claim to check", () => {
    expect(signupOutcome({ kind: "error", status: 403 })).toBe("locked");
  });

  it("never locks anyone out over a backend failure", () => {
    expect(signupOutcome({ kind: "error", status: 500 })).toBe("continue");
    expect(signupOutcome({ kind: "error", status: null })).toBe("continue");
  });
});
