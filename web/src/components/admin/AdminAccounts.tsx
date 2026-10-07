// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import Link from "next/link";

import { Pill } from "@/components/warm/Pill";
import { SERIF } from "@/components/warm/styles";
import {
  type AccountRow,
  type AdminView,
  CAP_UNCONFIGURED,
  DASH,
  type Filter,
  type Me,
  REVOKE_LAG,
  type Roster,
  STATUS_ORDER,
  type SeatPanel,
  type Summary,
  canGrant,
  canRevoke,
  confirmMatches,
  filterAccounts,
  fmtWhen,
  isMe,
  issueLabel,
  orDash,
  seatsValue,
  statusInfo,
} from "@/lib/adminAccounts";

/** Everything the page owns for granting and revoking: the open dialog, the
 *  in-flight mutation's state, and the last success message. */
export type SeatControls = {
  me: Me;
  panel: SeatPanel | null;
  pending: boolean;
  error: string | null;
  notice: string | null;
  onPanel: (panel: SeatPanel | null) => void;
  onSubmit: () => void;
};

const MUTED = { color: "var(--ink-4)" };
const PANEL = "rounded-[18px] border px-6 py-6 text-[13.5px]";
const PANEL_STYLE = {
  background: "var(--surface-warm)",
  borderColor: "var(--border-warm-hair)",
};

/** Every state of `/app/admin`. Wide screens get a table, narrow ones stacked
 *  cards; both are in the markup and CSS shows one. */
export function AdminAccounts({
  view,
  filter,
  onFilter,
  seats,
}: {
  view: AdminView;
  filter: Filter;
  onFilter: (f: Filter) => void;
  seats?: SeatControls;
}) {
  switch (view.kind) {
    case "loading":
      return (
        <div className="p-8 text-[13.5px]" style={MUTED}>
          Loading…
        </div>
      );
    case "not_found":
      return (
        <main className="mx-auto w-full max-w-[560px] flex-1 px-4 py-16 sm:px-7">
          <div className={PANEL} style={PANEL_STYLE}>
            <h1
              className="text-[26px] font-normal leading-[1.15]"
              style={{ fontFamily: SERIF, color: "var(--ink)" }}
            >
              This page doesn&apos;t exist.
            </h1>
            <Link href="/app" className="wm-link mt-4 inline-block font-bold">
              ← Back to Review
            </Link>
          </div>
        </main>
      );
    case "unavailable":
      return (
        <Shell>
          <div className={PANEL} style={PANEL_STYLE} data-state="unavailable">
            <p className="font-semibold" style={{ color: "var(--ink)" }}>
              Could not read{" "}
              {view.source ? <code>{view.source}</code> : "one of the account sources"}.
            </p>
            <p className="mt-2" style={MUTED}>
              The list is withheld rather than shown partially: a missing source
              would misreport every account. Try again in a minute.
            </p>
          </div>
        </Shell>
      );
    case "error":
      return (
        <Shell>
          <div className={PANEL} style={PANEL_STYLE} data-state="error">
            <p className="break-words font-semibold" style={{ color: "var(--brick)" }}>
              {view.message}
            </p>
            <p className="mt-2" style={MUTED}>
              Request id: <code className="break-all">{view.requestId ?? DASH}</code>
            </p>
          </div>
        </Shell>
      );
    case "empty":
      return (
        <Shell>
          <div className={PANEL} style={{ ...PANEL_STYLE, ...MUTED }}>
            No accounts yet.
          </div>
        </Shell>
      );
    case "list":
      return (
        <Shell roster={view.roster}>
          <AccountsList
            roster={view.roster}
            filter={filter}
            onFilter={onFilter}
            seats={seats}
          />
        </Shell>
      );
  }
}

function Shell({ roster, children }: { roster?: Roster; children: React.ReactNode }) {
  return (
    <main className="mx-auto w-full max-w-[1100px] flex-1 px-4 py-7 sm:px-7">
      <h1
        className="text-[28px] font-normal leading-[1.15]"
        style={{ fontFamily: SERIF, color: "var(--ink)" }}
      >
        Accounts
      </h1>
      {roster && (
        <div className="mt-2 text-[12.5px]" style={MUTED}>
          As of {fmtWhen(roster.generated_at)} · seat enforcement{" "}
          {roster.enforced ? "on" : "off"}
        </div>
      )}
      <div className="mt-5">{children}</div>
    </main>
  );
}

/** Summary strip, the All / Needs attention toggle, and the rows. */
export function AccountsList({
  roster,
  filter,
  onFilter,
  seats,
}: {
  roster: Roster;
  filter: Filter;
  onFilter: (f: Filter) => void;
  seats?: SeatControls;
}) {
  const rows = filterAccounts(roster.accounts, filter);
  const s = roster.summary;
  const extra = Object.keys(s.by_status).filter(
    (k) => !(STATUS_ORDER as string[]).includes(k),
  );
  return (
    <>
      <div className="flex flex-wrap gap-2.5" data-summary>
        <Stat label="Total" value={s.total} />
        <Stat label="Needs attention" value={s.needs_attention} strong={s.needs_attention > 0} />
        <Stat label="Seats" value={seatsValue(s)} />
      </div>
      {s.seat_cap == null && (
        <p
          className="mt-2 text-[12.5px] font-semibold"
          style={{ color: "var(--brick)" }}
          data-cap-unconfigured
        >
          {CAP_UNCONFIGURED}
        </p>
      )}
      {seats?.notice && (
        <p
          role="status"
          className="mt-3 rounded-[12px] px-3 py-2 text-[12.5px] font-semibold"
          style={{ color: "var(--sage)", background: "var(--sage-tint)" }}
          data-seat-notice
        >
          {seats.notice}
        </p>
      )}
      {seats?.panel && <SeatDialog seats={seats} panel={seats.panel} summary={s} />}
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5 text-[12px]" style={MUTED}>
        {[...STATUS_ORDER, ...extra].map((k) => (
          <span key={k} data-status-count={k}>
            {statusInfo(k).label}{" "}
            <b style={{ color: s.by_status[k] ? "var(--ink)" : undefined }}>
              {s.by_status[k] ?? 0}
            </b>
          </span>
        ))}
      </div>

      <div
        className="mt-5 mb-4 inline-flex gap-[3px] rounded-[14px] p-1"
        style={{ background: "#f6ede1" }}
      >
        <ToggleBtn active={filter === "all"} onClick={() => onFilter("all")} label="All" count={s.total} />
        <ToggleBtn
          active={filter === "attention"}
          onClick={() => onFilter("attention")}
          label="Needs attention"
          count={s.needs_attention}
        />
      </div>

      {rows.length === 0 ? (
        <div className={PANEL} style={{ ...PANEL_STYLE, ...MUTED }}>
          Nothing needs attention.
        </div>
      ) : (
        <>
          <WideTable rows={rows} seats={seats} />
          <NarrowCards rows={rows} seats={seats} />
        </>
      )}
    </>
  );
}

function Stat({
  label,
  value,
  strong,
}: {
  label: string;
  value: number | string;
  strong?: boolean;
}) {
  return (
    <div
      className="min-w-[120px] rounded-[14px] border px-4 py-3"
      style={PANEL_STYLE}
    >
      <div className="text-[11.5px] font-semibold" style={MUTED}>
        {label}
      </div>
      <div
        className="mt-0.5 text-[22px] font-bold"
        style={{ color: strong ? "var(--terracotta)" : "var(--ink)" }}
      >
        {value}
      </div>
    </div>
  );
}

function ToggleBtn({
  active,
  onClick,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={
        active
          ? "wm-tab wm-tab-active inline-flex items-center gap-[7px] rounded-[11px] px-[15px] py-[7px] text-[13px]"
          : "wm-tab inline-flex items-center gap-[7px] rounded-[11px] px-[15px] py-[7px] text-[13px]"
      }
    >
      {label}
      <span className="text-[11.5px]" style={{ color: "#a3927f" }}>
        {count}
      </span>
    </button>
  );
}

/** Rows with issues carry a terracotta left rule. */
function attentionStyle(row: AccountRow) {
  return row.issues.length > 0
    ? { boxShadow: "inset 3px 0 0 var(--terracotta)" }
    : undefined;
}

function Identity({ row }: { row: AccountRow }) {
  return (
    <div className="min-w-0">
      <div className="break-all text-[13.5px] font-semibold" style={{ color: "var(--ink)" }}>
        {orDash(row.email)}
      </div>
      <div className="break-words text-[12.5px]" style={{ color: "var(--ink-3)" }}>
        {orDash(row.display_name)}
      </div>
      <div className="break-all text-[11.5px]" style={MUTED}>
        {orDash(row.uid)}
      </div>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const info = statusInfo(status);
  return <Pill tone={info.tone}>{info.label}</Pill>;
}

function Issues({ issues }: { issues: string[] }) {
  if (issues.length === 0) return <span style={MUTED}>{DASH}</span>;
  return (
    <ul className="flex flex-col gap-1">
      {issues.map((i) => (
        <li
          key={i}
          data-issue={i}
          className="rounded-[8px] px-2 py-1 text-[11.5px] font-semibold leading-snug"
          style={{ color: "var(--terracotta-d)", background: "var(--terracotta-tint)" }}
        >
          {issueLabel(i)}
        </li>
      ))}
    </ul>
  );
}

function Seat({ row }: { row: AccountRow }) {
  const seat = row.seat;
  if (!seat) return <span style={MUTED}>{DASH}</span>;
  return (
    <div className="text-[12px] leading-relaxed" style={{ color: "var(--ink-3)" }}>
      {seat.revoked ? (
        <div>
          <b>Revoked</b> {fmtWhen(seat.revoked_at)} by {orDash(seat.revoked_by)}
        </div>
      ) : (
        <div>
          <b>Active</b>
        </div>
      )}
      <div>
        Added {fmtWhen(seat.added_at)} by {orDash(seat.added_by)}
      </div>
      <div className="break-words" style={MUTED}>
        Note: {orDash(seat.note)}
      </div>
      {row.waitlist && (
        <div style={MUTED}>Waitlisted {fmtWhen(row.waitlist.first_seen)}</div>
      )}
    </div>
  );
}

function SeatOrWaitlist({ row }: { row: AccountRow }) {
  if (!row.seat && row.waitlist) {
    return (
      <div className="text-[12px] leading-relaxed" style={{ color: "var(--ink-3)" }}>
        <div>
          <b>Waitlist</b> since {fmtWhen(row.waitlist.first_seen)}
        </div>
        <div style={MUTED}>Source: {orDash(row.waitlist.source)}</div>
      </div>
    );
  }
  return <Seat row={row} />;
}

function onboarded(row: AccountRow): string {
  if (!row.profile) return DASH;
  return row.profile.onboarded ? "Yes" : "No";
}

const TH = "px-3 py-2.5 text-left text-[11px] font-bold uppercase tracking-[0.06em]";
const TD = "px-3 py-3 align-top";

function WideTable({ rows, seats }: { rows: AccountRow[]; seats?: SeatControls }) {
  return (
    <div
      className="hidden overflow-hidden rounded-[18px] border lg:block"
      style={PANEL_STYLE}
      data-layout="wide"
    >
      <table className="w-full table-fixed border-collapse">
        <thead>
          <tr style={{ color: "#a3927f", borderBottom: "1px solid #f0e3d3" }}>
            <th className={`${TH} w-[26%]`}>Account</th>
            <th className={`${TH} w-[13%]`}>Status</th>
            <th className={`${TH} w-[22%]`}>Issues</th>
            <th className={`${TH} w-[19%]`}>Seat</th>
            <th className={`${TH} w-[12%]`}>Last sign-in</th>
            <th className={`${TH} w-[8%]`}>Onboarded</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.key}
              data-attention={row.issues.length > 0 ? "true" : undefined}
              style={{ borderBottom: "1px solid #f0e3d3", ...attentionStyle(row) }}
            >
              <td className={TD}>
                <Identity row={row} />
              </td>
              <td className={TD}>
                <StatusPill status={row.status} />
              </td>
              <td className={TD}>
                <Issues issues={row.issues} />
              </td>
              <td className={TD}>
                <SeatOrWaitlist row={row} />
                <SeatActions row={row} seats={seats} />
              </td>
              <td className={`${TD} text-[12px]`} style={{ color: "var(--ink-3)" }}>
                {fmtWhen(row.auth?.last_sign_in_at)}
              </td>
              <td className={`${TD} text-[12px]`} style={{ color: "var(--ink-3)" }}>
                {onboarded(row)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NarrowCards({ rows, seats }: { rows: AccountRow[]; seats?: SeatControls }) {
  return (
    <div className="flex flex-col gap-3 lg:hidden" data-layout="narrow">
      {rows.map((row) => (
        <div
          key={row.key}
          data-attention={row.issues.length > 0 ? "true" : undefined}
          className="rounded-[18px] border px-4 py-4"
          style={{ ...PANEL_STYLE, ...attentionStyle(row) }}
        >
          <div className="flex items-start justify-between gap-3">
            <Identity row={row} />
            <StatusPill status={row.status} />
          </div>
          {row.issues.length > 0 && (
            <div className="mt-3">
              <Issues issues={row.issues} />
            </div>
          )}
          <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-[12px]">
            <dt style={MUTED}>Seat</dt>
            <dd className="min-w-0">
              <SeatOrWaitlist row={row} />
              <SeatActions row={row} seats={seats} />
            </dd>
            <dt style={MUTED}>Last sign-in</dt>
            <dd style={{ color: "var(--ink-3)" }}>{fmtWhen(row.auth?.last_sign_in_at)}</dd>
            <dt style={MUTED}>Onboarded</dt>
            <dd style={{ color: "var(--ink-3)" }}>{onboarded(row)}</dd>
          </dl>
        </div>
      ))}
    </div>
  );
}

const SMALL_BTN = "mt-2 h-[28px] rounded-[10px] px-3 text-[12px] font-semibold";

/** Grant on a login without an active seat; Revoke on an active seat, except
 *  the admin's own. Nothing at all without `seats`. */
function SeatActions({ row, seats }: { row: AccountRow; seats?: SeatControls }) {
  if (!seats) return null;
  if (canGrant(row)) {
    return (
      <button
        onClick={() => seats.onPanel({ kind: "grant", row, note: "" })}
        disabled={seats.pending}
        className={`wm-cta ${SMALL_BTN}`}
        data-seat-action="grant"
      >
        Grant seat
      </button>
    );
  }
  if (canRevoke(row, seats.me)) {
    return (
      <button
        onClick={() => seats.onPanel({ kind: "revoke", row, typed: "" })}
        disabled={seats.pending}
        className={`wm-danger ${SMALL_BTN}`}
        data-seat-action="revoke"
      >
        Revoke seat
      </button>
    );
  }
  if (row.seat && !row.seat.revoked && isMe(row, seats.me)) {
    return (
      <div className="mt-2 text-[11.5px]" style={MUTED} data-seat-self>
        Your seat — it can&apos;t be revoked from here.
      </div>
    );
  }
  return null;
}

/** The grant form or the typed-confirmation revoke, as a modal. */
export function SeatDialog({
  seats,
  panel,
  summary,
}: {
  seats: SeatControls;
  panel: SeatPanel;
  summary: Summary;
}) {
  const email = panel.row.email ?? "";
  const revoke = panel.kind === "revoke";
  const ready = revoke ? confirmMatches(panel.typed, email) : true;
  const label = revoke ? "Revoke seat" : "Grant seat";
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center px-4"
      style={{ background: "rgba(40, 28, 18, 0.35)" }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={label}
        data-seat-dialog={panel.kind}
        className="w-full max-w-[440px] rounded-[18px] border p-5 text-[13px]"
        style={{ background: "#fdf7ee", borderColor: "var(--border-warm-hair)" }}
      >
        <h2 className="text-[16px] font-bold" style={{ color: "var(--ink)" }}>
          {label}
        </h2>
        <p className="mt-1 break-all font-semibold" style={{ color: "var(--ink-2)" }}>
          {email}
        </p>

        {revoke ? (
          <>
            <p className="mt-3 leading-[1.6]" style={{ color: "var(--ink-2)" }}>
              Type <b className="break-all">{email}</b> to confirm.
            </p>
            <input
              value={panel.typed}
              onChange={(e) => seats.onPanel({ ...panel, typed: e.target.value })}
              placeholder={email}
              autoComplete="off"
              aria-label="Type the email to confirm"
              className="wm-input mt-2 h-[38px] w-full rounded-[12px] px-3 text-[13px] outline-none"
            />
            <p className="mt-2 text-[11.5px]" style={MUTED} data-revoke-lag>
              {REVOKE_LAG} Until then they can keep using the app.
            </p>
          </>
        ) : (
          <>
            <label className="mt-3 block text-[11.5px] font-semibold" style={MUTED}>
              Note (optional)
              <input
                value={panel.note}
                onChange={(e) => seats.onPanel({ ...panel, note: e.target.value })}
                maxLength={500}
                autoComplete="off"
                className="wm-input mt-1 h-[38px] w-full rounded-[12px] px-3 text-[13px] font-normal outline-none"
              />
            </label>
            <p className="mt-2 text-[11.5px]" style={MUTED} data-dialog-seats>
              Seats in use: {seatsValue(summary)}
            </p>
            {summary.seat_cap == null && (
              <p className="mt-1 text-[11.5px] font-semibold" style={{ color: "var(--brick)" }}>
                {CAP_UNCONFIGURED}
              </p>
            )}
          </>
        )}

        {seats.error && (
          <p
            role="alert"
            className="mt-3 break-words text-[12px] font-semibold"
            style={{ color: "var(--brick)" }}
            data-seat-error
          >
            {seats.error}
          </p>
        )}

        <div className="mt-4 flex gap-2">
          <button
            onClick={() => seats.onPanel(null)}
            disabled={seats.pending}
            className="wm-ghost h-[34px] flex-1 rounded-[12px] border text-[12.5px] font-semibold"
            style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
          >
            Cancel
          </button>
          <button
            onClick={seats.onSubmit}
            disabled={!ready || seats.pending}
            className={
              revoke
                ? "h-[34px] flex-1 rounded-[12px] text-[12.5px] font-semibold disabled:opacity-40"
                : "wm-cta h-[34px] flex-1 rounded-[12px] text-[12.5px] font-semibold"
            }
            style={revoke ? { background: "var(--brick)", color: "#fff9f2" } : undefined}
            data-seat-submit
          >
            {seats.pending ? "Working…" : label}
          </button>
        </div>
      </div>
    </div>
  );
}
