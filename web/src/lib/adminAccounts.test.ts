import { describe, expect, it } from "vitest";

import {
  type AccountRow,
  ISSUE_LABEL,
  type Roster,
  STATUS_INFO,
  STATUS_ORDER,
  adminView,
  filterAccounts,
  fmtWhen,
  issueLabel,
  showsAdminNav,
  orDash,
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
