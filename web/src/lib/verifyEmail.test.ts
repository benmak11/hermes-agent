import { describe, expect, it } from "vitest";
import {
  canResend,
  describeVerifyError,
  isEmailUnverified,
  maskEmail,
  openVerify,
  RESEND_COOLDOWN_S,
  resendWaitSeconds,
  verifyReducer,
  verifyRedirect,
} from "@/lib/verifyEmail";

const UNVERIFIED = JSON.stringify({ detail: { reason: "email_unverified" } });

describe("maskEmail", () => {
  it("keeps the first letter and the domain", () => {
    expect(maskEmail("user@example.com")).toBe("u•••@example.com");
    expect(maskEmail("ab@example.com")).toBe("a•@example.com");
    expect(maskEmail("a@example.com")).toBe("a•@example.com");
  });

  it("never prints the local part beyond its first letter", () => {
    expect(maskEmail("someone.long@example.com")).not.toContain("someone");
  });

  it("falls back to plain words when there is nothing to mask", () => {
    expect(maskEmail(null)).toBe("your email address");
    expect(maskEmail("")).toBe("your email address");
    expect(maskEmail("@example.com")).toBe("your email address");
  });
});

describe("resend cooldown", () => {
  it("allows a send when none has been made", () => {
    expect(resendWaitSeconds(null, 1_000)).toBe(0);
  });

  it("counts down from the cooldown after a send", () => {
    expect(resendWaitSeconds(0, 0)).toBe(RESEND_COOLDOWN_S);
    expect(resendWaitSeconds(0, 15_000)).toBe(RESEND_COOLDOWN_S - 15);
    expect(resendWaitSeconds(0, RESEND_COOLDOWN_S * 1000)).toBe(0);
  });

  it("never reports more than the cooldown, even with a skewed clock", () => {
    expect(resendWaitSeconds(10_000, 0)).toBe(RESEND_COOLDOWN_S);
  });

  it("refuses a resend while busy or cooling down", () => {
    const fresh = openVerify("user@example.com", "signin");
    expect(canResend(fresh, 0)).toBe(true);
    expect(canResend({ ...fresh, busy: "sending" }, 0)).toBe(false);
    expect(canResend({ ...fresh, sentAt: 0 }, 1_000)).toBe(false);
    expect(canResend({ ...fresh, sentAt: 0 }, RESEND_COOLDOWN_S * 1000)).toBe(true);
  });
});

describe("verifyReducer", () => {
  const base = openVerify("user@example.com", "create", null);

  it("opens idle, with no message and the given send time", () => {
    expect(openVerify("user@example.com", "create", 5)).toEqual({
      email: "user@example.com",
      origin: "create",
      sentAt: 5,
      busy: "idle",
      message: null,
    });
  });

  it("records a send and starts the cooldown", () => {
    const s = verifyReducer(verifyReducer(base, { type: "send_start" }), {
      type: "sent",
      now: 42,
    });
    expect(s.busy).toBe("idle");
    expect(s.sentAt).toBe(42);
    expect(s.message?.tone).toBe("info");
  });

  it("maps a failed send to human copy and leaves the cooldown alone", () => {
    const s = verifyReducer(base, { type: "send_failed", code: "auth/too-many-requests" });
    expect(s.sentAt).toBeNull();
    expect(s.message).toEqual({
      tone: "error",
      text: describeVerifyError("auth/too-many-requests"),
    });
  });

  it("says plainly when the check finds no verification yet", () => {
    const checking = verifyReducer(base, { type: "check_start" });
    expect(checking.busy).toBe("checking");
    expect(checking.message).toBeNull();
    const s = verifyReducer(checking, { type: "still_unverified" });
    expect(s.busy).toBe("idle");
    expect(s.message?.text).toMatch(/can't see the verification yet/);
  });

  it("recovers from a failed check", () => {
    const s = verifyReducer(verifyReducer(base, { type: "check_start" }), {
      type: "check_failed",
    });
    expect(s.busy).toBe("idle");
    expect(s.message?.tone).toBe("error");
  });

  it("keeps the origin through every action", () => {
    const s = verifyReducer(base, { type: "sent", now: 1 });
    expect(s.origin).toBe("create");
  });
});

describe("describeVerifyError", () => {
  it("asks the user to wait on too-many-requests", () => {
    expect(describeVerifyError("auth/too-many-requests")).toMatch(/wait a few minutes/);
  });

  it("has a generic fallback with no error code in it", () => {
    const text = describeVerifyError("auth/something-new");
    expect(text).toBe("We couldn't send the email. Please try again.");
    expect(text).not.toContain("auth/");
  });
});

describe("isEmailUnverified", () => {
  it("recognises the verify refusal", () => {
    expect(isEmailUnverified(403, UNVERIFIED)).toBe(true);
  });

  it("does not mistake the allowlist refusal for it", () => {
    const allowlist = JSON.stringify({ detail: "this account is not on the allowlist" });
    expect(isEmailUnverified(403, allowlist)).toBe(false);
  });

  it("ignores other statuses and unparseable bodies", () => {
    expect(isEmailUnverified(401, UNVERIFIED)).toBe(false);
    expect(isEmailUnverified(403, "<html>")).toBe(false);
  });
});

describe("verifyRedirect", () => {
  it("sends an app page to the verify screen with a way back", () => {
    expect(verifyRedirect(403, UNVERIFIED, "/app/tracking", "?tab=2")).toBe(
      "/login?verify=1&next=%2Fapp%2Ftracking%3Ftab%3D2",
    );
  });

  it("never bounces the auth pages, which host the screen themselves", () => {
    expect(verifyRedirect(403, UNVERIFIED, "/login", "")).toBeNull();
    expect(verifyRedirect(403, UNVERIFIED, "/signup", "")).toBeNull();
  });

  it("leaves every other failure to the page", () => {
    expect(verifyRedirect(403, JSON.stringify({ detail: "nope" }), "/app", "")).toBeNull();
    expect(verifyRedirect(500, UNVERIFIED, "/app", "")).toBeNull();
  });
});
