import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import {
  AccountsList,
  AdminAccounts,
  SeatDialog,
  type SeatControls,
} from "@/components/admin/AdminAccounts";
import type { AccountRow, AdminView, Roster, SeatPanel } from "@/lib/adminAccounts";

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
    seat_cap: 25,
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
    expect(t).toMatch(/\|Seats\|+1 \/ 25\|/);
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

// ------------------------------------------------------------ seat controls

const SEAT = {
  revoked: false,
  added_at: "2026-09-01T09:00:00+00:00",
  added_by: "op",
  note: null,
  revoked_at: null,
  revoked_by: null,
};

const ME_ROW: AccountRow = {
  ...FINE,
  key: "uid:admin",
  uid: "admin",
  email: "admin@example.com",
  seat: SEAT,
};
const SEATED: AccountRow = { ...FINE, key: "uid:u3", uid: "u3", email: "seated@example.com", seat: SEAT };
const WAITING: AccountRow = {
  ...NEEDS,
  key: "uid:u4",
  uid: "u4",
  email: "waiting@example.com",
  status: "waitlisted",
  issues: [],
  waitlist: { first_seen: "2026-09-20T12:00:00+00:00", last_seen: null, source: "hero" },
};
const LAPSED: AccountRow = {
  ...FINE,
  key: "uid:u5",
  uid: "u5",
  email: "lapsed@example.com",
  status: "revoked",
  seat: { ...SEAT, revoked: true, revoked_at: "2026-09-30T00:00:00+00:00", revoked_by: "op" },
};

const SEAT_ROSTER: Roster = {
  ...ROSTER,
  // NEEDS (no_seat + login): grant. ORPHAN (seat, no login): revoke.
  // FINE (active, enforcement-off style, no seat doc): neither.
  accounts: [NEEDS, ORPHAN, FINE, ME_ROW, SEATED, WAITING, LAPSED],
};

function controls(over: Partial<SeatControls> = {}): SeatControls {
  return {
    me: { uid: "admin", email: "admin@example.com" },
    panel: null,
    pending: false,
    error: null,
    notice: null,
    onPanel: noop,
    onSubmit: noop,
    ...over,
  };
}

const seatList = (seats: SeatControls, roster: Roster = SEAT_ROSTER) =>
  renderToStaticMarkup(
    <AccountsList roster={roster} filter="all" onFilter={noop} seats={seats} />,
  );

/** The markup for one row in one layout, from its email to the next row. */
function rowBlock(html: string, which: "wide" | "narrow", email: string): string {
  const block = layout(html, which);
  const start = block.indexOf(email);
  expect(start, `${which} ${email}`).toBeGreaterThan(-1);
  const rowTag = which === "wide" ? "<tr" : 'class="rounded-[18px] border px-4 py-4"';
  const next = block.indexOf(rowTag, start);
  return next > -1 ? block.slice(start, next) : block.slice(start);
}

describe("seat actions", () => {
  it("Grant appears only on logins without an active seat", () => {
    const html = seatList(controls());
    for (const which of ["wide", "narrow"] as const) {
      const grants = layout(html, which).match(/data-seat-action="grant"/g) ?? [];
      expect(grants.length, which).toBe(3);
      for (const email of ["stray@example.com", "waiting@example.com", "lapsed@example.com"]) {
        expect(rowBlock(html, which, email), email).toContain('data-seat-action="grant"');
      }
    }
  });

  it("Revoke appears on active seats but never on the admin's own row", () => {
    const html = seatList(controls());
    for (const which of ["wide", "narrow"] as const) {
      const revokes = layout(html, which).match(/data-seat-action="revoke"/g) ?? [];
      expect(revokes.length, which).toBe(2);
      expect(rowBlock(html, which, "seated@example.com")).toContain('data-seat-action="revoke"');
      expect(rowBlock(html, which, "pending@example.com")).toContain('data-seat-action="revoke"');
      const mine = rowBlock(html, which, "admin@example.com");
      expect(mine).not.toContain("data-seat-action");
      expect(mine).toContain("data-seat-self");
    }
  });

  it("no actions at all without seat controls", () => {
    expect(list()).not.toContain("data-seat-action");
  });

  it("the success notice renders, with the 5-minute lag from the server", () => {
    const html = seatList(
      controls({ notice: "Seat revoked. It takes effect within 5 minutes: the API caches…" }),
    );
    expect(html).toMatch(/role="status"[^>]*data-seat-notice[^>]*>Seat revoked\. It takes effect within 5 minutes/);
  });
});

describe("seat summary", () => {
  it("shows Seats N / cap", () => {
    const t = text(seatList(controls()));
    expect(t).toMatch(/\|Seats\|+1 \/ 25\|/);
    expect(t).not.toContain("Seat cap not configured");
  });

  it("says when the cap is unconfigured", () => {
    const unset = { ...SEAT_ROSTER, summary: { ...SEAT_ROSTER.summary, seat_cap: null } };
    const html = seatList(controls(), unset);
    expect(text(html)).toMatch(/\|Seats\|+1 \/ —\|/);
    expect(html).toContain("data-cap-unconfigured");
    expect(html).toContain("Seat cap not configured — set MAX_USERS on the API to grant seats.");
  });
});

describe("SeatDialog", () => {
  const dialog = (panel: SeatPanel, over: Partial<SeatControls> = {}, seatCap: number | null = 25) =>
    renderToStaticMarkup(
      <SeatDialog
        seats={controls({ panel, ...over })}
        panel={panel}
        summary={{ ...ROSTER.summary, seat_cap: seatCap }}
      />,
    );
  const submit = (html: string) => {
    const m = html.match(/<button[^>]*data-seat-submit[^>]*>/);
    expect(m).not.toBeNull();
    return m![0];
  };
  const revoke = (typed: string) => ({ kind: "revoke" as const, row: SEATED, typed });

  it("keeps Revoke disabled until the typed email matches", () => {
    for (const typed of ["", "seated@", "seated@example.co", "seated@example.comx", "other@example.com"]) {
      expect(submit(dialog(revoke(typed))), typed).toMatch(/disabled=""/);
    }
    expect(submit(dialog(revoke("seated@example.com")))).not.toMatch(/disabled=""/);
    expect(submit(dialog(revoke(" Seated@Example.com ")))).not.toMatch(/disabled=""/);
  });

  it("disables Revoke while a request is in flight, even when matched", () => {
    expect(submit(dialog(revoke("seated@example.com"), { pending: true }))).toMatch(/disabled=""/);
  });

  it("the revoke dialog asks for the email and says it takes effect within 5 minutes", () => {
    const html = dialog(revoke(""));
    expect(html).toContain('role="dialog"');
    expect(html).toContain("Type <b");
    expect(html).toContain("A revoke takes effect within 5 minutes.");
  });

  it("the grant dialog has a note field and the seat count", () => {
    const html = dialog({ kind: "grant", row: WAITING, note: "" });
    expect(html).toContain("Note (optional)");
    expect(html).toContain("Seats in use: 1 / 25");
    expect(submit(html)).not.toMatch(/disabled=""/);
    expect(html).not.toContain("Seat cap not configured");
  });

  it("the grant dialog warns when the cap is unconfigured", () => {
    const html = dialog({ kind: "grant", row: WAITING, note: "" }, {}, null);
    expect(html).toContain("Seats in use: 1 / —");
    expect(html).toContain("Seat cap not configured — set MAX_USERS on the API to grant seats.");
  });

  it("renders the server's 409 cap message and other errors", () => {
    const html = dialog(
      { kind: "grant", row: WAITING, note: "" },
      { error: "seat cap reached: all 25 seats are in use. Revoke one or raise MAX_USERS first." },
    );
    expect(html).toMatch(/role="alert"[^>]*data-seat-error[^>]*>seat cap reached: all 25 seats/);
    expect(dialog(revoke("x"), { error: "type the email exactly to confirm the revoke" })).toContain(
      "type the email exactly to confirm the revoke",
    );
  });

  it("the list renders the open dialog", () => {
    const html = seatList(controls({ panel: revoke("") }));
    expect(html).toContain('data-seat-dialog="revoke"');
  });
});
