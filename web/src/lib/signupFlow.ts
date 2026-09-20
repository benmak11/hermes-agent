// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/** Pure post-auth admission logic — no imports, so it stays testable under vitest's node env. */

export type SignupResult =
  | { kind: "ok"; allowed: boolean }
  | { kind: "error"; status: number | null }; // null = network / non-HTTP failure

export type SignupOutcome = "continue" | "waitlist" | "locked" | "fallback";

/** Map POST /account/signup's answer to what the auth card does next. */
export function signupOutcome(r: SignupResult): SignupOutcome {
  if (r.kind === "ok") return r.allowed ? "continue" : "waitlist";
  if (r.status === 404) return "fallback"; // route not deployed yet
  if (r.status === 403) return "locked"; // token has no email claim to check
  return "continue"; // 500, network, …: the backend, not the allowlist — never lock out
}
