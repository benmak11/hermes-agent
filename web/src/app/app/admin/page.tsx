// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AdminAccounts, type SeatControls } from "@/components/admin/AdminAccounts";
import { TopNav } from "@/components/TopNav";
import { SERIF } from "@/components/warm/styles";
import {
  DASH,
  type Filter,
  type GrantResponse,
  type RevokeResponse,
  type Roster,
  type SeatPanel,
  adminView,
  fmtWhen,
  orDash,
  seatError,
  showsAdminNav,
} from "@/lib/adminAccounts";
import {
  type BoardHealth,
  type BoardRow,
  type BoardsView,
  NO_BOARDS,
  boardsSummary,
  boardsView,
  isFailing,
  listLabel,
  notFoundDays,
  outcomeLabel,
  stateLabel,
} from "@/lib/adminBoards";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/** Operator view of every account, with seat grant and revoke, then board
 *  health. The API answers anyone but the admin with a 404, which renders as
 *  this page's own not-found state. */
export default function AdminPage() {
  const { user, loading } = useAuth();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<Filter>("all");
  const [panel, setPanel] = useState<SeatPanel | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const { data, error, isPending } = useQuery({
    queryKey: ["admin", "accounts"],
    queryFn: () => apiFetch<Roster>("/admin/accounts"),
    enabled: !!user,
    retry: false,
    // Drop the roster (email addresses) from the cache as soon as the page unmounts.
    gcTime: 0,
  });

  const done = (message: string) => {
    setPanel(null);
    setNotice(message);
    queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
  };

  const grant = useMutation({
    mutationFn: (p: { email: string; note: string }) =>
      apiFetch<GrantResponse>("/admin/seats", {
        method: "POST",
        body: JSON.stringify({ email: p.email, note: p.note.trim() || null }),
      }),
    onSuccess: (res) => done(`Seat granted to ${res.email}.`),
  });

  const revoke = useMutation({
    mutationFn: (p: { email: string; confirm: string }) =>
      apiFetch<RevokeResponse>("/admin/seats/revoke", {
        method: "POST",
        body: JSON.stringify(p),
      }),
    onSuccess: (res) => done(res.message),
  });

  const active = panel?.kind === "revoke" ? revoke : grant;

  const seats: SeatControls | undefined = user
    ? {
        me: { uid: user.uid, email: user.email },
        panel,
        pending: grant.isPending || revoke.isPending,
        error: panel && active.isError ? seatError(active.error) : null,
        notice,
        onPanel: (next) => {
          // A fresh dialog starts without the last attempt's error.
          if (next?.row.key !== panel?.row.key || next?.kind !== panel?.kind) {
            grant.reset();
            revoke.reset();
          }
          setPanel(next);
        },
        onSubmit: () => {
          if (!panel?.row.email) return;
          setNotice(null);
          if (panel.kind === "grant") {
            grant.mutate({ email: panel.row.email, note: panel.note });
          } else {
            revoke.mutate({ email: panel.row.email, confirm: panel.typed });
          }
        },
      }
    : undefined;

  const view = adminView({ loading: loading || !user || isPending, error, data });

  // Asked for only once the accounts call has shown this is the admin.
  const boards = useQuery({
    queryKey: ["admin", "boards"],
    queryFn: () => apiFetch<BoardHealth>("/admin/boards"),
    enabled: !!user && showsAdminNav(view),
    retry: false,
  });
  const boardView = boardsView({
    loading: boards.isPending,
    error: boards.error,
    data: boards.data,
  });

  return (
    <>
      {showsAdminNav(view) && <TopNav section="admin" />}
      <div className="flex-1">
        <AdminAccounts view={view} filter={filter} onFilter={setFilter} seats={seats} />
        {showsAdminNav(view) && <BoardHealthSection view={boardView} />}
      </div>
    </>
  );
}

const MUTED = { color: "var(--ink-4)" };
const PANEL = "rounded-[18px] border px-6 py-6 text-[13.5px]";
const PANEL_STYLE = {
  background: "var(--surface-warm)",
  borderColor: "var(--border-warm-hair)",
};
const TH = "px-3 py-2.5 text-left text-[11px] font-bold uppercase tracking-[0.06em]";
const TD = "px-3 py-3 align-top text-[12px]";
const ROW_RULE = { borderBottom: "1px solid #f0e3d3" };

/** Shared per-board fetch health, view-only, failing boards first. */
function BoardHealthSection({ view }: { view: BoardsView }) {
  if (view.kind === "hidden") return null;
  return (
    <section className="mx-auto w-full max-w-[1100px] px-4 pb-10 sm:px-7" data-board-health>
      <h2
        className="text-[22px] font-normal leading-[1.15]"
        style={{ fontFamily: SERIF, color: "var(--ink)" }}
      >
        Board health
      </h2>
      {(view.kind === "list" || view.kind === "empty") && (
        <p className="mt-2 text-[12.5px]" style={MUTED} data-board-summary>
          {boardsSummary(view.totals)}
        </p>
      )}
      <div className="mt-4">
        <BoardHealthBody view={view} />
      </div>
    </section>
  );
}

function BoardHealthBody({ view }: { view: BoardsView }) {
  switch (view.kind) {
    case "hidden":
      return null;
    case "loading":
      return (
        <div className="text-[13.5px]" style={MUTED}>
          Loading…
        </div>
      );
    case "unavailable":
      return (
        <div className={PANEL} style={PANEL_STYLE} data-state="unavailable">
          <p className="font-semibold" style={{ color: "var(--ink)" }}>
            Could not read {view.source ? <code>{view.source}</code> : "board health"}.
          </p>
          <p className="mt-2" style={MUTED}>
            Try again in a minute.
          </p>
        </div>
      );
    case "error":
      return (
        <div className={PANEL} style={PANEL_STYLE} data-state="error">
          <p className="break-words font-semibold" style={{ color: "var(--brick)" }}>
            {view.message}
          </p>
          <p className="mt-2" style={MUTED}>
            Request id: <code className="break-all">{view.requestId ?? DASH}</code>
          </p>
        </div>
      );
    case "empty":
      return (
        <div className={PANEL} style={{ ...PANEL_STYLE, ...MUTED }}>
          {NO_BOARDS}
        </div>
      );
    case "list":
      return <BoardTable rows={view.rows} />;
  }
}

function BoardTable({ rows }: { rows: BoardRow[] }) {
  return (
    <div className="overflow-x-auto rounded-[18px] border" style={PANEL_STYLE}>
      <table className="w-full min-w-[860px] border-collapse">
        <thead>
          <tr style={{ color: "#a3927f", ...ROW_RULE }}>
            <th className={TH}>Board</th>
            <th className={TH}>Name</th>
            <th className={TH}>List</th>
            <th className={TH}>State</th>
            <th className={TH}>Last outcome</th>
            <th className={TH}>404 days</th>
            <th className={TH}>Failing since</th>
            <th className={TH}>Last ok</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={`${row.platform}:${row.slug}`}
              data-failing={isFailing(row) ? "true" : undefined}
              style={{
                ...ROW_RULE,
                ...(isFailing(row) ? { boxShadow: "inset 3px 0 0 var(--brick)" } : {}),
              }}
            >
              <td className={TD}>
                <div style={MUTED}>{row.platform}</div>
                <div className="break-all font-semibold" style={{ color: "var(--ink)" }}>
                  {row.slug}
                </div>
              </td>
              <td className={TD} style={{ color: "var(--ink-3)" }}>
                {orDash(row.name)}
              </td>
              <td className={TD} style={{ color: "var(--ink-3)" }}>
                {listLabel(row)}
              </td>
              <td
                className={`${TD} font-semibold`}
                style={{ color: isFailing(row) ? "var(--brick)" : "var(--ink-3)" }}
              >
                {stateLabel(row)}
              </td>
              <td className={TD} style={{ color: "var(--ink-3)" }}>
                {outcomeLabel(row)}
              </td>
              <td className={TD} style={{ color: "var(--ink-3)" }}>
                {notFoundDays(row)}
              </td>
              <td className={TD} style={{ color: "var(--ink-3)" }}>
                {fmtWhen(row.failing_since)}
              </td>
              <td className={TD} style={{ color: "var(--ink-3)" }}>
                {fmtWhen(row.last_ok_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
