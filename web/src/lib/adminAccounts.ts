// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { ApiError } from "@/lib/apiError";
import type { PillTone } from "@/components/warm/Pill";

/** Mirrors `tools/account/roster.py`; `adminAccounts.test.ts` checks the
 *  status and issue values against that file. */
export type Status =
  | "deleted"
  | "data_without_login"
  | "disabled"
  | "active"
  | "seat_without_login"
  | "waitlisted"
  | "revoked"
  | "no_seat";

export type Issue =
  | "login_without_seat"
  | "seat_unused"
  | "seat_on_deleted_account"
  | "deletion_unfinished"
  | "data_without_login"
  | "waitlist_without_login"
  | "unverified_email"
  | "no_email"
  | "duplicate_email";

export type SeatInfo = {
  revoked: boolean;
  added_at: string | null;
  added_by: string | null;
  note: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
};

export type WaitlistInfo = {
  first_seen: string | null;
  last_seen: string | null;
  source: string | null;
};

export type AuthInfo = {
  created_at: string | null;
  last_sign_in_at: string | null;
  email_verified: boolean;
  disabled: boolean;
};

export type ProfileInfo = {
  onboarded: boolean;
  signup_source: string | null;
  signed_up_at: string | null;
  deleted_at: string | null;
};

/** `status` and `issues` stay `string` on the wire: a value newer than this
 *  build must still render (as its raw key), not crash or go blank. */
export type AccountRow = {
  key: string;
  uid: string | null;
  email: string | null;
  display_name: string | null;
  status: string;
  issues: string[];
  seat: SeatInfo | null;
  waitlist: WaitlistInfo | null;
  auth: AuthInfo | null;
  profile: ProfileInfo | null;
};

export type Summary = {
  total: number;
  needs_attention: number;
  seats_active: number;
  by_status: Record<string, number>;
};

export type Roster = {
  generated_at: string;
  enforced: boolean;
  summary: Summary;
  accounts: AccountRow[];
};

export const STATUS_INFO: Record<Status, { label: string; tone: PillTone }> = {
  active: { label: "Active", tone: "good" },
  seat_without_login: { label: "Seat, no login", tone: "warn" },
  waitlisted: { label: "Waitlisted", tone: "accent" },
  revoked: { label: "Revoked", tone: "muted" },
  no_seat: { label: "No seat", tone: "warn" },
  disabled: { label: "Disabled", tone: "muted" },
  deleted: { label: "Deleted", tone: "muted" },
  data_without_login: { label: "Data, no login", tone: "warn" },
};

export const ISSUE_LABEL: Record<Issue, string> = {
  login_without_seat: "Login without a seat — enforcement refuses this account",
  seat_unused: "Seat granted, not signed in since",
  seat_on_deleted_account: "Seat on a deleted account",
  deletion_unfinished: "Deletion didn't finish",
  data_without_login: "Data with no login",
  waitlist_without_login: "On the waitlist, never signed in",
  unverified_email: "Email not verified",
  no_email: "No email on the login",
  duplicate_email: "Email shared with another login",
};

/** Statuses in the backend's precedence order, for the summary strip. */
export const STATUS_ORDER = Object.keys(STATUS_INFO) as Status[];

export function statusInfo(status: string): { label: string; tone: PillTone } {
  return STATUS_INFO[status as Status] ?? { label: status, tone: "muted" };
}

export function issueLabel(issue: string): string {
  return ISSUE_LABEL[issue as Issue] ?? issue;
}

export type Filter = "all" | "attention";

/** The toggle's rows, in the backend's order (needs-attention first). */
export function filterAccounts(rows: AccountRow[], filter: Filter): AccountRow[] {
  return filter === "attention" ? rows.filter((r) => r.issues.length > 0) : rows;
}

export const DASH = "—";

/** `2026-10-05 14:32 UTC`; `—` for a missing value, the raw text if unparseable. */
export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function orDash(value: string | null | undefined): string {
  return value ? value : DASH;
}

export type AdminView =
  | { kind: "loading" }
  | { kind: "not_found" }
  | { kind: "unavailable"; source: string | null }
  | { kind: "error"; message: string; requestId: string | null }
  | { kind: "empty" }
  | { kind: "list"; roster: Roster };

/** The 503 body is `{"detail": "could not read <source>"}`. */
function unreadSource(body: string): string | null {
  try {
    const detail = (JSON.parse(body) as { detail?: unknown }).detail;
    if (typeof detail !== "string") return null;
    return detail.replace(/^could not read\s+/, "") || null;
  } catch {
    return null;
  }
}

/** Which state the admin page is in. `loading` covers auth and the fetch. */
export function adminView({
  loading,
  error,
  data,
}: {
  loading: boolean;
  error: unknown;
  data: Roster | undefined;
}): AdminView {
  if (error) {
    if (error instanceof ApiError) {
      if (error.status === 404) return { kind: "not_found" };
      if (error.status === 503) {
        return { kind: "unavailable", source: unreadSource(error.body) };
      }
      return { kind: "error", message: error.message, requestId: error.requestId };
    }
    return { kind: "error", message: String(error), requestId: null };
  }
  if (loading || !data) return { kind: "loading" };
  if (data.accounts.length === 0) return { kind: "empty" };
  return { kind: "list", roster: data };
}

/** Whether the page shows the nav, whose section label reads "Admin": not
 *  until the API has answered, and never on the not-found state. */
export function showsAdminNav(view: AdminView): boolean {
  return view.kind !== "loading" && view.kind !== "not_found";
}
