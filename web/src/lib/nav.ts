// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/** Pure route helpers — no imports, so they stay testable under vitest's node env. */

export const APP_HOME = "/app";
export const LOGIN_PATH = "/login";
export const SIGNUP_FROM_KEY = "hermes:signupFrom"; // sessionStorage, consumed by PR 4

const SIGNUP_SOURCES = ["hero", "closing", "nav", "footer"] as const;
export type SignupSource = (typeof SIGNUP_SOURCES)[number];

/** `?next=` guard: only same-app paths come back; anything else is null. */
export function safeNext(raw: string | null): string | null {
  if (!raw) return null;
  if (!raw.startsWith(APP_HOME)) return null; // rejects schemes, "//", "/login", "/"
  const tail = raw.charAt(APP_HOME.length);
  if (tail !== "" && tail !== "/" && tail !== "?") return null; // rejects "/apple", "/app%2F.."
  if (raw.includes("\\") || raw.includes("//") || raw.includes("..")) return null;
  if (/[\r\n]/.test(raw)) return null;
  // The literal checks above miss percent-encoded dot segments ("/app/%2e%2e/login"
  // resolves to "/login"). Let the URL parser normalise and re-check the invariant.
  let parsed: URL;
  try {
    parsed = new URL(raw, "http://h");
  } catch {
    return null;
  }
  if (parsed.origin !== "http://h") return null;
  if (parsed.pathname !== APP_HOME && !parsed.pathname.startsWith(APP_HOME + "/")) return null;
  return raw;
}

export function signupFrom(raw: string | null): SignupSource | null {
  return (SIGNUP_SOURCES as readonly string[]).includes(raw ?? "") ? (raw as SignupSource) : null;
}

/** Where the /app gate sends a signed-out visitor; `search` includes its leading "?". */
export function loginHref(pathname: string, search: string): string {
  return `${LOGIN_PATH}?next=${encodeURIComponent(pathname + search)}`;
}
