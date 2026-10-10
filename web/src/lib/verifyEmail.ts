// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/**
 * Pure logic for the verify-your-email screen — no imports, so it stays
 * testable under vitest's node env. An email/password account must verify its
 * address before the API lets it in (403 `{"reason": "email_unverified"}`,
 * or `{"allowed": false, "reason": "verify_email"}` from POST /account/signup).
 */

/** Seconds between "Resend email" clicks. Firebase rate-limits too; this keeps
 *  the button from inviting a `too-many-requests`. */
export const RESEND_COOLDOWN_S = 60;

/** Where the screen was opened from: what "verified" leads to differs. */
export type VerifyOrigin = "create" | "signin" | "app";

export type VerifyState = {
  email: string | null;
  origin: VerifyOrigin;
  /** ms epoch of the last verification email sent from this screen, if any. */
  sentAt: number | null;
  busy: "idle" | "checking" | "sending";
  message: { tone: "info" | "error"; text: string } | null;
};

export type VerifyAction =
  | { type: "sent"; now: number }
  | { type: "send_start" }
  | { type: "send_failed"; code: string }
  | { type: "check_start" }
  | { type: "still_unverified" }
  | { type: "check_failed" };

export function openVerify(
  email: string | null,
  origin: VerifyOrigin,
  sentAt: number | null = null,
): VerifyState {
  return { email, origin, sentAt, busy: "idle", message: null };
}

export function verifyReducer(state: VerifyState, action: VerifyAction): VerifyState {
  switch (action.type) {
    case "send_start":
      return { ...state, busy: "sending", message: null };
    case "sent":
      return {
        ...state,
        busy: "idle",
        sentAt: action.now,
        message: { tone: "info", text: "Sent. It can take a minute to arrive — check spam too." },
      };
    case "send_failed":
      return {
        ...state,
        busy: "idle",
        message: { tone: "error", text: describeVerifyError(action.code) },
      };
    case "check_start":
      return { ...state, busy: "checking", message: null };
    case "still_unverified":
      return {
        ...state,
        busy: "idle",
        message: {
          tone: "error",
          text: "We can't see the verification yet. Open the link in the email, then try again.",
        },
      };
    case "check_failed":
      return {
        ...state,
        busy: "idle",
        message: { tone: "error", text: "Something went wrong checking. Please try again." },
      };
  }
}

/** Seconds until "Resend email" may be clicked again; 0 means now. */
export function resendWaitSeconds(sentAt: number | null, now: number): number {
  if (sentAt === null) return 0;
  const left = RESEND_COOLDOWN_S - Math.floor((now - sentAt) / 1000);
  return Math.max(0, Math.min(RESEND_COOLDOWN_S, left));
}

/** Can the resend button fire? Not mid-request, and not inside the cooldown. */
export function canResend(state: VerifyState, now: number): boolean {
  return state.busy === "idle" && resendWaitSeconds(state.sentAt, now) === 0;
}

/** "u•••@example.com": enough to recognise, not enough to harvest. */
export function maskEmail(email: string | null | undefined): string {
  if (!email) return "your email address";
  const at = email.lastIndexOf("@");
  if (at <= 0) return "your email address";
  const local = email.slice(0, at);
  return `${local[0]}${"•".repeat(Math.min(Math.max(local.length - 1, 1), 3))}${email.slice(at)}`;
}

/** Firebase `sendEmailVerification` error code → copy a human can act on. */
export function describeVerifyError(code: string): string {
  switch (code) {
    case "auth/too-many-requests":
      return "We've sent several emails already. Please wait a few minutes before asking for another.";
    case "auth/network-request-failed":
      return "We couldn't reach the server. Check your connection and try again.";
    case "auth/user-token-expired":
    case "auth/requires-recent-login":
    case "auth/user-disconnected":
      return "Your session expired. Use a different account, sign in again, then resend.";
    default:
      return "We couldn't send the email. Please try again.";
  }
}

/** Is this failed API response the "verify your email first" refusal? */
export function isEmailUnverified(status: number, body: string): boolean {
  if (status !== 403) return false;
  try {
    const detail = (JSON.parse(body) as { detail?: { reason?: unknown } }).detail;
    return detail?.reason === "email_unverified";
  } catch {
    return false;
  }
}

/** The auth pages host the verify screen themselves; never bounce them. */
const AUTH_PATHS = ["/login", "/signup"];

/** Where an in-app `email_unverified` 403 sends the browser, or null to stay. */
export function verifyRedirect(
  status: number,
  body: string,
  pathname: string,
  search: string,
): string | null {
  if (!isEmailUnverified(status, body)) return null;
  if (AUTH_PATHS.includes(pathname)) return null;
  return `/login?verify=1&next=${encodeURIComponent(pathname + search)}`;
}
