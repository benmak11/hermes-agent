import { describe, expect, it } from "vitest";

import {
  type AccountRow,
  ISSUE_LABEL,
  type Roster,
  STATUS_INFO,
  STATUS_ORDER,
  CAP_UNCONFIGURED,
  adminView,
  canGrant,
  canRevoke,
  confirmMatches,
  filterAccounts,
  fmtWhen,
  issueLabel,
  showsAdminNav,
  orDash,
  seatError,
  seatsValue,
  statusInfo,
} from "@/lib/adminAccounts";
import { ApiError } from "@/lib/apiError";

function row(over: Partial<AccountRow>): AccountRow {
  return {
    key: "uid:x",
    uid: "x",
    email: "x@example.com",
    display_name: null,
    status: "active",
    issues: [],
    seat: null,
    waitlist: null,
    auth: null,
    profile: null,
    ...over,
  };
}

function roster(accounts: AccountRow[]): Roster {
  return {
    generated_at: "2026-10-06T12:00:00+00:00",
    enforced: true,
    summary: {
      total: accounts.length,
      needs_attention: accounts.filter((a) => a.issues.length).length,
      seats_active: 0,
      seat_cap: 25,
      by_status: {},
    },
    accounts,
  };
}

describe("labels", () => {
  it("every status has a non-empty label and a tone", () => {
    for (const s of STATUS_ORDER) {
      expect(STATUS_INFO[s].label.trim(), s).not.toBe("");
      expect(["accent", "good", "warn", "muted"]).toContain(STATUS_INFO[s].tone);
    }
  });

  it("every issue has a non-empty label", () => {
    for (const [k, v] of Object.entries(ISSUE_LABEL)) expect(v.trim(), k).not.toBe("");
  });

  it("an unknown value renders as its raw key, never blank", () => {
    expect(statusInfo("brand_new")).toEqual({ label: "brand_new", tone: "muted" });
    expect(issueLabel("brand_new")).toBe("brand_new");
    expect(statusInfo("active").label).toBe("Active");
    expect(issueLabel("seat_unused")).toBe("Seat granted, not signed in since");
  });
});

describe("filterAccounts", () => {
  const rows = [
    row({ key: "a", issues: ["seat_unused"] }),
    row({ key: "b", issues: ["no_email", "unverified_email"] }),
    row({ key: "c" }),
  ];

  it("all keeps every row in the backend's order", () => {
    expect(filterAccounts(rows, "all").map((r) => r.key)).toEqual(["a", "b", "c"]);
  });

  it("attention keeps only rows with issues, order preserved", () => {
    expect(filterAccounts(rows, "attention").map((r) => r.key)).toEqual(["a", "b"]);
  });
});

describe("formatting", () => {
  it("renders nulls as an em dash", () => {
    expect(fmtWhen(null)).toBe("—");
    expect(fmtWhen(undefined)).toBe("—");
    expect(fmtWhen("")).toBe("—");
    expect(orDash(null)).toBe("—");
    expect(orDash("")).toBe("—");
    expect(orDash("a")).toBe("a");
  });

  it("formats an ISO time in UTC, and passes junk through", () => {
    expect(fmtWhen("2026-10-05T14:32:09.123+00:00")).toBe("2026-10-05 14:32 UTC");
    expect(fmtWhen("not a date")).toBe("not a date");
  });
});

describe("adminView", () => {
  const data = roster([row({})]);

  it("is loading while auth or the fetch is pending", () => {
    expect(adminView({ loading: true, error: null, data: undefined })).toEqual({ kind: "loading" });
    expect(adminView({ loading: false, error: null, data: undefined })).toEqual({ kind: "loading" });
  });

  it("treats a 404 as not found, not as an error", () => {
    const error = new ApiError(404, '{"detail":"Not Found"}', "req-1");
    expect(adminView({ loading: false, error, data: undefined })).toEqual({ kind: "not_found" });
  });

  it("names the source a 503 could not read", () => {
    const error = new ApiError(503, '{"detail":"could not read firebase_auth"}', "req-2");
    expect(adminView({ loading: false, error, data: undefined })).toEqual({
      kind: "unavailable",
      source: "firebase_auth",
    });
  });

  it("a 503 with an unparseable body is still withheld, source unknown", () => {
    const error = new ApiError(503, "Service Unavailable", "req-3");
    expect(adminView({ loading: false, error, data: undefined })).toEqual({
      kind: "unavailable",
      source: null,
    });
  });

  it("any other error carries the message and request id", () => {
    const error = new ApiError(500, "boom", "req-4");
    expect(adminView({ loading: false, error, data: undefined })).toEqual({
      kind: "error",
      message: "500: boom (request req-4)",
      requestId: "req-4",
    });
    expect(adminView({ loading: false, error: new Error("offline"), data: undefined })).toEqual({
      kind: "error",
      message: "Error: offline",
      requestId: null,
    });
  });

  it("an error wins over a stale loading flag", () => {
    const error = new ApiError(404, "", "r");
    expect(adminView({ loading: true, error, data: undefined }).kind).toBe("not_found");
  });

  it("is empty with zero accounts, and the list otherwise", () => {
    expect(adminView({ loading: false, error: null, data: roster([]) })).toEqual({ kind: "empty" });
    expect(adminView({ loading: false, error: null, data })).toEqual({ kind: "list", roster: data });
  });
});

describe("showsAdminNav", () => {
  it("hides the Admin-labelled nav until the API answers, and on not found", () => {
    expect(showsAdminNav({ kind: "loading" })).toBe(false);
    expect(showsAdminNav({ kind: "not_found" })).toBe(false);
    expect(showsAdminNav({ kind: "unavailable", source: null })).toBe(true);
    expect(showsAdminNav({ kind: "error", message: "m", requestId: null })).toBe(true);
    expect(showsAdminNav({ kind: "empty" })).toBe(true);
  });
});

const AUTH = {
  created_at: "2026-09-01T10:00:00+00:00",
  last_sign_in_at: null,
  email_verified: true,
  disabled: false,
};
const SEAT = {
  revoked: false,
  added_at: "2026-09-01T10:00:00+00:00",
  added_by: "op",
  note: null,
  revoked_at: null,
  revoked_by: null,
};
const ME = { uid: "admin", email: "Admin@Example.com" };

describe("canGrant", () => {
  it("offers Grant on a login that is waitlisted, seatless or revoked", () => {
    for (const status of ["waitlisted", "no_seat", "revoked"]) {
      const seat = status === "revoked" ? { ...SEAT, revoked: true } : null;
      expect(canGrant(row({ status, auth: AUTH, seat })), status).toBe(true);
    }
  });

  it("never on an active seat, a row without a login, or other statuses", () => {
    expect(canGrant(row({ status: "active", auth: AUTH, seat: SEAT }))).toBe(false);
    // An active seat wins over a grantable status.
    expect(canGrant(row({ status: "waitlisted", auth: AUTH, seat: SEAT }))).toBe(false);
    expect(canGrant(row({ status: "waitlisted", auth: null, uid: null }))).toBe(false);
    expect(canGrant(row({ status: "no_seat", auth: AUTH, email: null }))).toBe(false);
    for (const status of ["deleted", "disabled", "data_without_login", "seat_without_login"]) {
      expect(canGrant(row({ status, auth: AUTH })), status).toBe(false);
    }
  });
});

describe("canRevoke", () => {
  it("offers Revoke on any active seat, with or without a login", () => {
    expect(canRevoke(row({ status: "active", auth: AUTH, seat: SEAT }), ME)).toBe(true);
    expect(
      canRevoke(row({ status: "seat_without_login", uid: null, seat: SEAT }), ME),
    ).toBe(true);
  });

  it("never on a revoked seat or no seat", () => {
    expect(canRevoke(row({ seat: { ...SEAT, revoked: true } }), ME)).toBe(false);
    expect(canRevoke(row({ seat: null }), ME)).toBe(false);
  });

  it("never on the admin's own row, matched by uid or by email", () => {
    expect(canRevoke(row({ uid: "admin", email: "other@x.com", seat: SEAT }), ME)).toBe(
      false,
    );
    expect(canRevoke(row({ uid: null, email: " admin@example.COM", seat: SEAT }), ME)).toBe(
      false,
    );
  });
});

describe("confirmMatches", () => {
  it("is false until the typed text is the email", () => {
    expect(confirmMatches("", "a@x.com")).toBe(false);
    expect(confirmMatches("a@x.co", "a@x.com")).toBe(false);
    expect(confirmMatches("a@x.comm", "a@x.com")).toBe(false);
    expect(confirmMatches("a@x.com", "a@x.com")).toBe(true);
  });

  it("ignores case and surrounding space, like the server, but never matches empty", () => {
    expect(confirmMatches("  A@X.com ", "a@x.com")).toBe(true);
    expect(confirmMatches("", "")).toBe(false);
    expect(confirmMatches("   ", null)).toBe(false);
  });
});

describe("seats", () => {
  const summary = (seat_cap: number | null) => ({
    total: 9,
    needs_attention: 0,
    seats_active: 7,
    by_status: {},
    seat_cap,
  });

  it("reads N / cap, and a dash when the cap is unconfigured", () => {
    expect(seatsValue(summary(25))).toBe("7 / 25");
    expect(seatsValue(summary(null))).toBe("7 / —");
  });

  it("seatError shows the server's detail, and the not-configured message for that 503", () => {
    const cap = new ApiError(
      409,
      JSON.stringify({ detail: "seat cap reached: all 25 seats are in use." }),
      "r1",
    );
    expect(seatError(cap)).toBe("seat cap reached: all 25 seats are in use.");
    const unset = new ApiError(
      503,
      JSON.stringify({ detail: "seat cap not configured: set MAX_USERS" }),
      "r2",
    );
    expect(seatError(unset)).toBe(CAP_UNCONFIGURED);
    expect(seatError(new ApiError(500, "boom", "r3"))).toBe("500: boom (request r3)");
    expect(seatError(new Error("offline"))).toBe("Error: offline");
  });
});
