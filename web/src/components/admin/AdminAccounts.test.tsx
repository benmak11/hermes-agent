import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { AccountsList, AdminAccounts } from "@/components/admin/AdminAccounts";
import type { AccountRow, AdminView, Roster } from "@/lib/adminAccounts";

const noop = () => {};

const NEEDS: AccountRow = {
  key: "uid:u1",
  uid: "u1",
  email: "stray@example.com",
  display_name: null,
  status: "no_seat",
  issues: ["login_without_seat", "unverified_email"],
  seat: null,
  waitlist: null,
  auth: {
    created_at: "2026-09-01T10:00:00+00:00",
    last_sign_in_at: null,
    email_verified: false,
    disabled: false,
  },
  profile: null,
};

const ORPHAN: AccountRow = {
  key: "email:pending@example.com",
  uid: null,
  email: "pending@example.com",
  display_name: null,
  status: "seat_without_login",
  issues: [],
  seat: {
    revoked: false,
    added_at: "2026-10-01T09:00:00+00:00",
    added_by: null,
    note: null,
    revoked_at: null,
    revoked_by: null,
  },
  waitlist: null,
  auth: null,
  profile: null,
};

const FINE: AccountRow = {
  key: "uid:u2",
  uid: "u2",
  email: "ok@example.com",
  display_name: "Okay Person",
  status: "active",
  issues: [],
  seat: null,
  waitlist: null,
  auth: {
    created_at: "2026-09-01T10:00:00+00:00",
    last_sign_in_at: "2026-10-05T14:32:00+00:00",
    email_verified: true,
    disabled: false,
  },
  profile: { onboarded: true, signup_source: null, signed_up_at: null, deleted_at: null },
};

const ROSTER: Roster = {
  generated_at: "2026-10-06T12:00:00+00:00",
  enforced: true,
  summary: {
    total: 3,
    needs_attention: 1,
    seats_active: 1,
    by_status: {
      deleted: 0,
      data_without_login: 0,
      disabled: 0,
      active: 1,
      seat_without_login: 1,
      waitlisted: 0,
      revoked: 0,
      no_seat: 1,
    },
  },
  accounts: [NEEDS, ORPHAN, FINE],
};

const list = (filter: "all" | "attention" = "all") =>
  renderToStaticMarkup(<AccountsList roster={ROSTER} filter={filter} onFilter={noop} />);

/** The markup of one layout's block, so a row can be checked per layout. */
function layout(html: string, which: "wide" | "narrow"): string {
  const start = html.indexOf(`data-layout="${which}"`);
  expect(start, which).toBeGreaterThan(-1);
  const other = html.indexOf(`data-layout="${which === "wide" ? "narrow" : "wide"}"`);
  return other > start ? html.slice(start, other) : html.slice(start);
}

const text = (html: string) => html.replace(/<[^>]+>/g, "|");

describe("AccountsList", () => {
  it("renders both the wide table and the narrow cards, one hidden by CSS", () => {
    const html = list();
    expect(html).toMatch(/class="hidden [^"]*lg:block"[^>]*data-layout="wide"/);
    expect(html).toMatch(/class="[^"]*lg:hidden"[^>]*data-layout="narrow"/);
    expect(layout(html, "wide")).toContain("<table");
    expect(layout(html, "narrow")).not.toContain("<table");
  });

  it("renders nulls as an em dash, never blank, null or undefined", () => {
    const html = list();
    expect(html).not.toMatch(/>null<|>undefined<|\bnull\b|\bundefined\b/);
    for (const which of ["wide", "narrow"] as const) {
      const block = layout(html, which);
      const t = text(block);
      // NEEDS: no name, never signed in, no profile.
      expect(t, which).toContain("|stray@example.com||—||u1|");
      // ORPHAN: no name, no uid, seat added_by and note null.
      expect(t, which).toContain("|pending@example.com||—||—|");
      expect(t, which).toContain("by —|");
      expect(t, which).toContain("Note: —|");
      // No cell, definition or identity line is left empty.
      expect(block, which).not.toMatch(/<(td|dd)[^>]*><\/\1>/);
      expect(block, which).not.toMatch(/<div class="break-[^"]*"[^>]*><\/div>/);
    }
    const wide = text(layout(html, "wide"));
    // NEEDS' last sign-in and onboarded cells.
    expect(wide).toMatch(/Email not verified\|+—\|+—\|+—\|/);
  });

  it("renders an issue badge per issue, with its label", () => {
    const html = list();
    for (const which of ["wide", "narrow"] as const) {
      const block = layout(html, which);
      expect(block).toContain('data-issue="login_without_seat"');
      expect(block).toContain("Login without a seat — enforcement refuses this account");
      expect(block).toContain('data-issue="unverified_email"');
      expect(block).toContain("Email not verified");
    }
  });

  it("marks only the needs-attention row", () => {
    const html = list();
    for (const which of ["wide", "narrow"] as const) {
      const block = layout(html, which);
      expect((block.match(/data-attention="true"/g) ?? []).length, which).toBe(1);
      const marked = block.slice(block.indexOf('data-attention="true"'));
      expect(marked).toContain("inset 3px 0 0 var(--terracotta)");
      expect(marked.indexOf("stray@example.com")).toBeLessThan(marked.indexOf("ok@example.com"));
    }
  });

  it("shows status pills, sign-in time and onboarded", () => {
    const t = text(list());
    expect(t).toContain("|Active|");
    expect(t).toContain("|No seat|");
    expect(t).toContain("|Seat, no login|");
    expect(t).toContain("|2026-10-05 14:32 UTC|");
    expect(t).toContain("|Yes|");
  });

  it("the summary strip shows totals and a count per status", () => {
    const html = list();
    const t = text(html);
    expect(t).toMatch(/\|Total\|+3\|/);
    expect(t).toMatch(/\|Needs attention\|+1\|/);
    expect(t).toMatch(/\|Active seats\|+1\|/);
    for (const k of Object.keys(ROSTER.summary.by_status)) {
      expect(html).toContain(`data-status-count="${k}"`);
    }
  });

  it("the Needs attention toggle filters out rows without issues", () => {
    const all = list("all");
    const attention = list("attention");
    expect(all).toContain("ok@example.com");
    expect(all).toContain("pending@example.com");
    expect(attention).toContain("stray@example.com");
    expect(attention).not.toContain("ok@example.com");
    expect(attention).not.toContain("pending@example.com");
    expect(attention).toMatch(/aria-pressed="true"[^>]*>Needs attention/);
  });
});

describe("AdminAccounts states", () => {
  const render = (view: AdminView) =>
    renderToStaticMarkup(<AdminAccounts view={view} filter="all" onFilter={noop} />);

  it("loading", () => {
    expect(render({ kind: "loading" })).toContain("Loading…");
  });

  it("not found links back to /app and says nothing about accounts", () => {
    const html = render({ kind: "not_found" });
    expect(html).toContain("This page doesn&#x27;t exist.");
    expect(html).toContain('href="/app"');
    expect(html).not.toContain("Accounts");
  });

  it("unavailable names the source and says the list is withheld", () => {
    const html = render({ kind: "unavailable", source: "firebase_auth" });
    expect(html).toContain("<code>firebase_auth</code>");
    expect(html).toContain("withheld rather than shown partially");
    expect(html).not.toContain("<table");
  });

  it("error shows the message and the request id", () => {
    const html = render({ kind: "error", message: "500: boom (request r-9)", requestId: "r-9" });
    expect(html).toContain("500: boom (request r-9)");
    expect(html).toMatch(/Request id: <code[^>]*>r-9<\/code>/);
  });

  it("empty", () => {
    expect(render({ kind: "empty" })).toContain("No accounts yet.");
  });

  it("list renders the roster", () => {
    const html = render({ kind: "list", roster: ROSTER });
    expect(html).toContain("Accounts");
    expect(html).toContain("seat enforcement on");
    expect(html).toContain("stray@example.com");
  });
});
