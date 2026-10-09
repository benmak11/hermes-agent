// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { DASH } from "@/lib/adminAccounts";
import { ApiError } from "@/lib/apiError";

/** One `board_health` record joined with the company lists; mirrors
 *  `BoardRow` in `api/routes/admin.py`. Enum-like fields stay `string` so a
 *  value newer than this build still renders as its raw key. */
export type BoardRow = {
  platform: string;
  slug: string;
  name: string | null;
  list: string;
  paused: boolean;
  blocklisted: boolean;
  state: string | null;
  last_outcome: string | null;
  last_status: number | null;
  failing_since: string | null;
  not_found_days: number;
  last_ok_at: string | null;
  updated_at: string | null;
  /** A moved board's target. */
  resolves_to?: { platform: string; slug: string } | null;
  /** What the last probe round found on other platforms. */
  candidates?: BoardCandidate[];
};

export type BoardCandidate = {
  platform: string;
  slug: string;
  name: string | null;
  job_count: number | null;
};

export type BoardTotals = {
  total: number;
  ok: number;
  failing: number;
  quarantined: number;
  dead: number;
  moved: number;
  not_found: number;
  never_checked: number;
};

export type BoardHealth = { totals: BoardTotals; boards: BoardRow[] };

export const NO_BOARDS = "No board checks recorded yet";

export function isFailing(row: BoardRow): boolean {
  return row.state === "failing";
}

/** Problem states first; mirrors `board_health.STATE_ORDER`. */
const STATE_ORDER = ["failing", "quarantined", "dead", "moved", "ok"];

function stateRank(row: BoardRow): number {
  const i = row.state ? STATE_ORDER.indexOf(row.state) : -1;
  return i === -1 ? STATE_ORDER.length : i;
}

/** How a state is shown: brick for a board that needs a human, the accent for
 *  one that moved, muted otherwise. */
export type StateTone = "brick" | "accent" | "muted";

export function stateTone(row: BoardRow): StateTone {
  if (row.state === "failing" || row.state === "quarantined" || row.state === "dead") {
    return "brick";
  }
  if (row.state === "moved") return "accent";
  return "muted";
}

export const TONE_COLOR: Record<StateTone, string> = {
  brick: "var(--brick)",
  accent: "var(--terracotta-d)",
  muted: "var(--ink-3)",
};

/** By state (failing, quarantined, dead, moved, ok, then anything newer),
 *  then most 404 days, then platform and slug — the server's order,
 *  reapplied so the table never depends on it. */
export function sortBoards(rows: BoardRow[]): BoardRow[] {
  return [...rows].sort(
    (a, b) =>
      stateRank(a) - stateRank(b) ||
      (b.not_found_days ?? 0) - (a.not_found_days ?? 0) ||
      a.platform.localeCompare(b.platform) ||
      a.slug.localeCompare(b.slug),
  );
}

/** "12 boards · 6 ok · 3 failing · 1 quarantined · 1 dead · 1 moved ·
 *  2 not found · 4 never checked". */
export function boardsSummary(t: BoardTotals): string {
  return [
    `${t.total} ${t.total === 1 ? "board" : "boards"}`,
    `${t.ok} ok`,
    `${t.failing} failing`,
    `${t.quarantined ?? 0} quarantined`,
    `${t.dead ?? 0} dead`,
    `${t.moved ?? 0} moved`,
    `${t.not_found} not found`,
    `${t.never_checked} never checked`,
  ].join(" · ");
}

const OUTCOME_LABEL: Record<string, string> = {
  ok: "OK",
  not_found: "Not found",
  rate_limited: "Rate limited",
  server_error: "Server error",
  timeout: "Timeout",
  error: "Error",
};

/** "Not found · 404"; the label alone without a status; `—` without either. */
export function outcomeLabel(row: BoardRow): string {
  const label = row.last_outcome ? (OUTCOME_LABEL[row.last_outcome] ?? row.last_outcome) : null;
  const status = row.last_status == null ? null : String(row.last_status);
  if (label && status) return `${label} · ${status}`;
  return label ?? status ?? DASH;
}

const LIST_LABEL: Record<string, string> = {
  known: "Known",
  unvetted: "Unvetted",
  none: "Not listed",
};

/** "Known", "Known · paused", "Not listed · blocklisted". */
export function listLabel(row: BoardRow): string {
  const parts = [LIST_LABEL[row.list] ?? row.list];
  if (row.paused) parts.push("paused");
  if (row.blocklisted) parts.push("blocklisted");
  return parts.join(" · ");
}

const STATE_LABEL: Record<string, string> = {
  ok: "OK",
  failing: "Failing",
  quarantined: "Quarantined",
  dead: "Dead",
  moved: "Moved",
};

export function stateLabel(row: BoardRow): string {
  if (!row.state) return DASH;
  return STATE_LABEL[row.state] ?? row.state;
}

/** A moved board's target ("→ lever/acme"), a quarantined board's candidate
 *  names ("Acme Inc, lever/acme"), else `—`. A nameless candidate shows as
 *  its board. */
export function resolutionLabel(row: BoardRow): string {
  if (row.state === "moved" && row.resolves_to) {
    return `→ ${row.resolves_to.platform}/${row.resolves_to.slug}`;
  }
  if (row.state === "quarantined" && row.candidates?.length) {
    return row.candidates.map((c) => c.name ?? `${c.platform}/${c.slug}`).join(", ");
  }
  return DASH;
}

/** A count of 404 days; `—` for none. */
export function notFoundDays(row: BoardRow): string {
  return row.not_found_days > 0 ? String(row.not_found_days) : DASH;
}

export type BoardsView =
  | { kind: "loading" }
  | { kind: "hidden" }
  | { kind: "unavailable"; source: string | null }
  | { kind: "error"; message: string; requestId: string | null }
  | { kind: "empty"; totals: BoardTotals }
  | { kind: "list"; totals: BoardTotals; rows: BoardRow[] };

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

/** Which state the board-health section is in. A 404 (not the admin) hides
 *  it: the accounts section already shows the not-found page. */
export function boardsView({
  loading,
  error,
  data,
}: {
  loading: boolean;
  error: unknown;
  data: BoardHealth | undefined;
}): BoardsView {
  if (error) {
    if (error instanceof ApiError) {
      if (error.status === 404) return { kind: "hidden" };
      if (error.status === 503) {
        return { kind: "unavailable", source: unreadSource(error.body) };
      }
      return { kind: "error", message: error.message, requestId: error.requestId };
    }
    return { kind: "error", message: String(error), requestId: null };
  }
  if (loading || !data) return { kind: "loading" };
  if (data.boards.length === 0) return { kind: "empty", totals: data.totals };
  return { kind: "list", totals: data.totals, rows: sortBoards(data.boards) };
}
