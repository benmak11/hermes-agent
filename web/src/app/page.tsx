// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  clearFirstRun,
  loadStats,
  paceMinutes,
  recordDecision,
  revertDecision,
  reviewedCount,
  saveMinScore,
  saveStats,
  useFirstRun,
  useMinScore,
  useSessionStats,
  type SessionStats,
} from "@/lib/session";
import type { DecideValue, Decision, Job, ProfileResponse } from "@/lib/types";
import { avatarColor, barColor, initial, recPill, scoreColor } from "@/lib/ui";
import { TopNav } from "@/components/TopNav";

type PendingResponse = { jobs: Job[] };

/** A decision in its 6s soft-commit window (mock 07): undoable until it lands. */
type PendingCommit = { job: Job; decision: Decision };

const SOFT_COMMIT_MS = 6000;

const DECISION_VERB: Record<Decision, string> = {
  approved: "Approved",
  rejected: "Skipped",
  starred: "Starred",
};

export default function VettingPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();
  const uid = user?.uid ?? null;
  // Storage-backed values (hydration-safe external stores).
  const minScore = useMinScore();
  const stats = useSessionStats(uid);
  const firstRun = useFirstRun();
  const [pending, setPending] = useState<PendingCommit | null>(null);
  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<PendingCommit | null>(null);
  // Landed decisions this session, newest last — `z` walks back through them
  // even after the soft-commit window (server-side revert).
  const historyRef = useRef<PendingCommit[]>([]);

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

  // First-run gate: a user with no profile yet is sent to onboarding before
  // they ever see the (empty) job queue.
  const { data: profileData, isLoading: profileLoading } = useQuery({
    queryKey: ["profile-gate"],
    queryFn: () => apiFetch<ProfileResponse>("/profile"),
    enabled: !!user,
  });
  const needsOnboarding = profileData && profileData.profile === null;

  useEffect(() => {
    if (needsOnboarding) router.push("/onboarding");
  }, [needsOnboarding, router]);

  const queryKey = ["pending", minScore] as const;

  const { data, isLoading, error } = useQuery({
    queryKey,
    queryFn: () =>
      apiFetch<PendingResponse>(`/jobs/pending?min_score=${minScore}`),
    enabled: !!user && profileData?.profile != null,
    // Matches stream in as agents write them — poll faster while discovery is
    // fresh after onboarding, gently otherwise.
    refetchInterval: firstRun ? 5000 : 30000,
  });

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: DecideValue }) =>
      apiFetch(`/jobs/${id}/decide`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      }),
    onError: () => {
      // A failed hard-commit refetches so the card comes back — the decision
      // is still pending server-side and isn't silently lost.
      queryClient.invalidateQueries({ queryKey: ["pending"] });
    },
  });
  // useMutation's `mutate` is referentially stable; the mutation object is not.
  const mutateDecide = decide.mutate;

  const bumpStats = useCallback(
    (fn: (s: SessionStats) => SessionStats) => {
      if (uid) saveStats(uid, fn(loadStats(uid)));
    },
    [uid],
  );

  const commitNow = useCallback(() => {
    const p = pendingRef.current;
    if (!p) return;
    if (commitTimer.current) clearTimeout(commitTimer.current);
    commitTimer.current = null;
    pendingRef.current = null;
    setPending(null);
    mutateDecide({ id: p.job.id, decision: p.decision });
    historyRef.current.push(p);
  }, [mutateDecide]);

  const softDecide = useCallback(
    (job: Job, decision: Decision) => {
      // One soft-commit window at a time: a new decision lands the previous one.
      if (pendingRef.current) commitNow();
      queryClient.setQueryData<PendingResponse>(queryKey, (old) =>
        old ? { jobs: old.jobs.filter((j) => j.id !== job.id) } : old,
      );
      const p = { job, decision };
      pendingRef.current = p;
      setPending(p);
      bumpStats((s) => recordDecision(s, decision));
      commitTimer.current = setTimeout(commitNow, SOFT_COMMIT_MS);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [commitNow, queryClient, minScore, bumpStats],
  );

  const undo = useCallback(() => {
    const restore = (p: PendingCommit) => {
      queryClient.setQueryData<PendingResponse>(queryKey, (old) =>
        old
          ? { jobs: [p.job, ...old.jobs.filter((j) => j.id !== p.job.id)] }
          : old,
      );
      bumpStats((s) => revertDecision(s, p.decision));
    };

    const p = pendingRef.current;
    if (p) {
      // Still in the soft-commit window: cancel before it reaches the server.
      if (commitTimer.current) clearTimeout(commitTimer.current);
      commitTimer.current = null;
      pendingRef.current = null;
      setPending(null);
      restore(p);
      return;
    }

    // Already landed — walk the session history and revert server-side
    // (mock 03: z reverses the last decision any time this session).
    const last = historyRef.current.pop();
    if (!last) return;
    mutateDecide({ id: last.job.id, decision: "pending" });
    restore(last);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryClient, minScore, bumpStats, mutateDecide]);

  // Leaving the page lands any decision still in its window (unmount only —
  // a ref keeps render-to-render identity churn from flushing the timer).
  const commitRef = useRef(commitNow);
  useEffect(() => {
    commitRef.current = commitNow;
  }, [commitNow]);
  useEffect(() => () => commitRef.current(), []);

  const jobs = data?.jobs ?? [];
  const top = jobs[0];

  const act = useCallback(
    (decision: Decision) => {
      if (top) softDecide(top, decision);
    },
    [top, softDecide],
  );

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (
        e.target instanceof HTMLElement &&
        (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")
      )
        return;
      if (e.key === "a") act("approved");
      else if (e.key === "s") act("rejected");
      else if (e.key === "r") act("starred");
      else if (e.key === "z") undo();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [act, undo]);

  // The first-run treatment retires once the queue is real and being worked.
  useEffect(() => {
    if (firstRun && reviewedCount(stats) >= 3) clearFirstRun();
  }, [firstRun, stats]);

  if (loading || !user || profileLoading || needsOnboarding) {
    return <div className="p-8" style={{ color: "var(--muted)" }}>Loading…</div>;
  }

  const reviewed = reviewedCount(stats);
  const remaining = jobs.length;
  const total = reviewed + remaining;
  const pace = paceMinutes(stats, remaining);

  return (
    <>
      <TopNav
        section="review"
        center={
          reviewed > 0 && total > 0 ? (
            <SessionProgress reviewed={reviewed} total={total} />
          ) : undefined
        }
        pill={firstRun ? <DiscoveryPill /> : undefined}
      />
      <main className="mx-auto w-full max-w-[760px] flex-1 px-8 py-7">
        <div className="mb-6 flex items-start justify-between gap-5">
          <div>
            <div className="flex items-center gap-2.5">
              <h1
                className="text-[22px] font-semibold tracking-tight"
                style={{ color: "var(--text)" }}
              >
                {firstRun ? "Your first matches" : "Jobs to review"}
              </h1>
              {!firstRun && (
                <span
                  className="inline-flex h-[22px] min-w-6 items-center justify-center rounded-full px-[7px] font-mono text-xs font-semibold"
                  style={{ background: "var(--text)", color: "var(--surface)" }}
                >
                  {jobs.length}
                </span>
              )}
            </div>
            {firstRun ? (
              <div
                className="mt-2 font-mono text-[12.5px] font-medium"
                style={{ color: "var(--muted)" }}
              >
                <span style={{ color: "var(--good)", fontWeight: 600 }}>
                  {jobs.length} {jobs.length === 1 ? "match" : "matches"}
                </span>{" "}
                so far — start reviewing, more will appear below
              </div>
            ) : (
              <div
                className="mt-2 flex items-center gap-1.5 text-xs"
                style={{ color: "var(--muted)" }}
              >
                <Kbd>a</Kbd> approve <Kbd>s</Kbd> skip <Kbd>r</Kbd> star{" "}
                <Kbd>z</Kbd> undo
              </div>
            )}
          </div>

          <label
            className="flex h-[38px] items-center gap-2.5 rounded-[10px] border px-3.5"
            style={{ background: "var(--surface)", borderColor: "var(--border)" }}
          >
            <span
              className="whitespace-nowrap text-xs font-medium"
              style={{ color: "var(--muted)" }}
            >
              Min score
            </span>
            <input
              type="range"
              min={0}
              max={100}
              value={minScore}
              onChange={(e) => saveMinScore(Number(e.target.value))}
              className="w-[130px] accent-[var(--accent)]"
            />
            <span
              className="w-5 text-right font-mono text-[13px] font-semibold tabular-nums"
              style={{ color: "var(--text)" }}
            >
              {minScore}
            </span>
          </label>
        </div>

        {isLoading && <LoadingSkeleton />}
        {error && (
          <p style={{ color: "var(--danger)" }}>Failed to load: {String(error)}</p>
        )}
        {!isLoading && jobs.length === 0 && !firstRun && (
          <EmptyState minScore={minScore} onLower={() => saveMinScore(0)} />
        )}

        <div className="space-y-4">
          {jobs.map((job, i) => (
            <JobCard
              key={job.id}
              job={job}
              isTop={i === 0}
              onDecide={(d) => softDecide(job, d)}
            />
          ))}
        </div>

        {firstRun && !isLoading && <ScoringCard />}

        {reviewed > 0 && (
          <div
            className="mt-4 flex items-center gap-3.5 font-mono text-xs font-medium"
            style={{ color: "var(--muted)" }}
          >
            <CountDot
              color="var(--good)"
              label={`${stats.approved} approved`}
              href="/tracking"
            />
            <CountDot
              color="var(--border-mid)"
              label={`${stats.skipped} skipped`}
              href="/tracking?tab=skipped"
            />
            <CountDot
              color="var(--star)"
              label={`${stats.starred} starred`}
              href="/tracking?tab=starred"
            />
            <span className="ml-auto" style={{ color: "var(--subtle)" }}>
              {remaining} remaining
              {pace != null ? ` · ~${pace} min at your pace` : ""}
            </span>
          </div>
        )}
      </main>

      {pending && <UndoToast pending={pending} onUndo={undo} />}
    </>
  );
}

function SessionProgress({
  reviewed,
  total,
}: {
  reviewed: number;
  total: number;
}) {
  const pct = Math.min(100, Math.round((reviewed / total) * 100));
  return (
    <div className="flex items-center gap-[11px]">
      <div
        className="h-[5px] w-[150px] overflow-hidden rounded-full"
        style={{ background: "var(--surface-2)" }}
      >
        <div
          className="h-full rounded-full"
          style={{ width: `${pct}%`, background: "var(--good)" }}
        />
      </div>
      <span
        className="font-mono text-xs font-semibold"
        style={{ color: "var(--label)" }}
      >
        {reviewed} of {total} reviewed
      </span>
    </div>
  );
}

function DiscoveryPill() {
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border px-3 py-[5px] font-mono text-xs font-semibold"
      style={{
        background: "var(--accent-bg)",
        borderColor: "var(--accent-border)",
        color: "var(--accent-text)",
      }}
    >
      <span
        className="inline-block h-[11px] w-[11px] rounded-full border-2"
        style={{
          borderColor: "var(--accent-text)",
          borderTopColor: "transparent",
          animation: "hspin 0.8s linear infinite",
        }}
      />
      discovery running
    </span>
  );
}

/** Dashed placeholder for a match still being scored (mock 06). */
function ScoringCard() {
  return (
    <div
      className="mt-4 rounded-[14px] px-[22px] py-5"
      style={{
        border: "1px dashed var(--border-mid)",
        background: "color-mix(in srgb, var(--surface) 60%, transparent)",
      }}
    >
      <div className="flex items-center gap-[11px]">
        <div
          className="h-[34px] w-[34px] rounded-lg h-pulse"
          style={{ background: "var(--surface-2)" }}
        />
        <div className="flex-1">
          <div
            className="h-[13px] w-[220px] rounded h-pulse"
            style={{ background: "var(--surface-2)" }}
          />
          <div
            className="mt-[7px] h-[11px] w-[110px] rounded h-pulse"
            style={{ background: "var(--divider)" }}
          />
        </div>
        <span
          className="font-mono text-[11px] font-medium"
          style={{ color: "var(--subtle)" }}
        >
          scoring…
        </span>
      </div>
    </div>
  );
}

function CountDot({
  color,
  label,
  href,
}: {
  color: string;
  label: string;
  href?: string;
}) {
  const body = (
    <>
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      {label}
    </>
  );
  if (href) {
    return (
      <Link href={href} className="inline-flex items-center gap-1.5">
        {body}
      </Link>
    );
  }
  return <span className="inline-flex items-center gap-1.5">{body}</span>;
}

/**
 * Bottom-center undo toast (mock 07): dark pill with the decision, an Undo
 * button carrying the `z` kbd, and a 20px SVG ring draining over the 6s
 * soft-commit window.
 */
function UndoToast({
  pending,
  onUndo,
}: {
  pending: PendingCommit;
  onUndo: () => void;
}) {
  return (
    <div
      key={pending.job.id}
      className="h-slideup fixed bottom-[22px] left-1/2 z-20 flex -translate-x-1/2 items-center gap-3.5 rounded-xl py-[11px] pl-4 pr-3"
      style={{
        background: "var(--toast-bg)",
        boxShadow: "0 10px 30px rgba(0,0,0,0.3)",
        animationDuration: "0.3s",
      }}
    >
      <span className="text-[13px]" style={{ color: "var(--toast-text)" }}>
        {DECISION_VERB[pending.decision]}{" "}
        <b style={{ color: "var(--toast-strong)" }}>{pending.job.title}</b> ·{" "}
        {pending.job.company}
      </span>
      <button
        onClick={onUndo}
        className="inline-flex h-[30px] items-center gap-1.5 rounded-lg border px-3 text-[12.5px] font-semibold"
        style={{
          background: "var(--toast-btn-bg)",
          borderColor: "var(--toast-btn-border)",
          color: "var(--toast-btn-text)",
        }}
      >
        Undo{" "}
        <kbd
          className="inline-flex h-[17px] min-w-4 items-center justify-center rounded border px-1 font-mono text-[10px] font-semibold"
          style={{
            background: "var(--toast-kbd-bg)",
            borderColor: "var(--toast-kbd-border)",
            color: "var(--toast-kbd-text)",
          }}
        >
          z
        </kbd>
      </button>
      <svg width="20" height="20" viewBox="0 0 20 20" style={{ transform: "rotate(-90deg)" }}>
        <circle
          cx="10"
          cy="10"
          r="8"
          fill="none"
          stroke="var(--toast-ring-track)"
          strokeWidth="2.5"
        />
        <circle
          cx="10"
          cy="10"
          r="8"
          fill="none"
          stroke="var(--toast-ring)"
          strokeWidth="2.5"
          strokeDasharray="50.3"
          style={{ animation: `ringDrain ${SOFT_COMMIT_MS}ms linear both` }}
        />
      </svg>
    </div>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <kbd
      className="inline-flex h-[19px] min-w-[18px] items-center justify-center rounded-[5px] border px-[5px] font-mono text-[11px] font-semibold"
      style={{
        background: "var(--surface-2)",
        borderColor: "var(--border)",
        color: "var(--label)",
      }}
    >
      {children}
    </kbd>
  );
}

function JobCard({
  job,
  isTop,
  onDecide,
}: {
  job: Job;
  isTop: boolean;
  onDecide: (decision: Decision) => void;
}) {
  const [open, setOpen] = useState(false);
  const m = job.match;
  const av = avatarColor(job.company);
  const pill = recPill(m.recommendation);
  const dealHits = Math.max(0, Math.round(100 - m.breakdown.deal_breaker_penalty));

  return (
    <article
      className="h-slideup rounded-[14px] border p-[22px]"
      style={{
        background: "var(--surface)",
        borderColor: isTop ? "var(--border-strong)" : "var(--border)",
        boxShadow: isTop ? "0 0 0 3px var(--ring)" : "none",
      }}
    >
      <div className="flex items-start justify-between gap-5">
        <div className="min-w-0">
          <div className="flex items-center gap-[11px]">
            <span
              className="flex h-[34px] w-[34px] flex-none items-center justify-center rounded-lg text-[15px] font-bold"
              style={{ background: av.bg, color: av.color }}
            >
              {initial(job.company)}
            </span>
            <div className="min-w-0">
              <h2
                className="truncate text-[17px] font-semibold tracking-tight"
                style={{ color: "var(--text)" }}
              >
                {job.title}
              </h2>
              <div className="text-[13px]" style={{ color: "var(--muted)" }}>
                {job.company}
              </div>
            </div>
          </div>
          <div
            className="mt-3 flex flex-wrap items-center gap-2 font-mono text-xs font-medium"
            style={{ color: "var(--muted)" }}
          >
            <span>{job.location ?? "—"}</span>
            <Dot />
            <span>{job.source}</span>
            <Dot />
            <a
              href={job.url}
              target="_blank"
              rel="noreferrer"
              className="font-semibold"
              style={{ color: "var(--accent)" }}
            >
              View posting ↗
            </a>
            {job.discovered_via === "unvetted" && (
              <span
                className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[11px] font-semibold"
                style={{ background: "var(--first-bg)", color: "var(--first-text)" }}
              >
                ★ First-time company
              </span>
            )}
          </div>
        </div>

        <div className="flex-shrink-0 text-right">
          <div className="flex items-baseline justify-end gap-0.5">
            <span
              className="text-[38px] font-bold leading-none tracking-tight tabular-nums"
              style={{ color: scoreColor(m.recommendation) }}
            >
              {Math.round(m.overall_score)}
            </span>
            <span className="text-sm font-medium" style={{ color: "var(--subtle)" }}>
              /100
            </span>
          </div>
          <span
            className="mt-2.5 inline-flex items-center gap-1.5 rounded-full border px-[9px] py-[3px] font-mono text-[11px] font-semibold tracking-wide"
            style={{ background: pill.bg, borderColor: pill.border, color: pill.color }}
          >
            <span
              className="inline-block h-1.5 w-1.5 rounded-full"
              style={{ background: pill.dot }}
            />
            {pill.label}
          </span>
        </div>
      </div>

      <p
        className="mt-4 text-sm leading-relaxed"
        style={{ color: "var(--label)" }}
      >
        {m.reasoning}
      </p>

      {open && (
        <div className="mt-4 flex flex-col gap-[11px]">
          <Bar label="role fit" value={m.breakdown.role_fit} />
          <Bar label="qualifications" value={m.breakdown.qualifications_match} />
          <Bar label="seniority" value={m.breakdown.seniority_match} />
          <Bar label="comp alignment" value={m.breakdown.comp_alignment} />
          <Bar
            label="deal-breakers"
            value={dealHits}
            fill="var(--danger)"
            goodWhenZero
          />
          {m.matched_strengths.length > 0 && (
            <ChipRow label="Strengths" color="var(--good)" items={m.matched_strengths} variant="good" />
          )}
          {m.gaps.length > 0 && (
            <ChipRow label="Gaps" color="var(--warn)" items={m.gaps} variant="warn" />
          )}
          {m.red_flags_hit.length > 0 && (
            <ChipRow label="Red flags" color="var(--danger)" items={m.red_flags_hit} variant="warn" />
          )}
        </div>
      )}

      <div className="my-[18px] h-px" style={{ background: "var(--divider)" }} />

      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <button
            onClick={() => onDecide("approved")}
            className="inline-flex h-[38px] items-center gap-2 rounded-[9px] px-[18px] text-[13px] font-semibold"
            style={{ background: "var(--text)", color: "var(--surface)" }}
          >
            ✓ Approve
          </button>
          <GhostBtn onClick={() => onDecide("rejected")}>→ Skip</GhostBtn>
          <GhostBtn onClick={() => onDecide("starred")}>★ Star</GhostBtn>
        </div>
        <button
          onClick={() => setOpen((o) => !o)}
          className="inline-flex items-center gap-1.5 text-[13px] font-medium"
          style={{ color: open ? "var(--text)" : "var(--muted)" }}
        >
          {open ? "▾" : "▸"} Breakdown
        </button>
      </div>
    </article>
  );
}

function Dot() {
  return <span style={{ color: "var(--border-strong)", opacity: 0.4 }}>·</span>;
}

function GhostBtn({
  onClick,
  children,
}: {
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className="inline-flex h-[38px] items-center gap-1.5 rounded-[9px] border px-4 text-[13px] font-semibold"
      style={{
        background: "var(--surface)",
        borderColor: "var(--border)",
        color: "var(--label)",
      }}
    >
      {children}
    </button>
  );
}

function Bar({
  label,
  value,
  fill,
  goodWhenZero,
}: {
  label: string;
  value: number;
  fill?: string;
  goodWhenZero?: boolean;
}) {
  const v = Math.max(0, Math.min(100, Math.round(value)));
  const color = fill ?? barColor(v);
  const valueColor = goodWhenZero && v === 0 ? "var(--good)" : "var(--text)";
  return (
    <div className="flex items-center gap-3.5">
      <span
        className="w-[118px] font-mono text-xs font-medium"
        style={{ color: "var(--label)" }}
      >
        {label}
      </span>
      <div
        className="h-[7px] flex-1 overflow-hidden rounded-full"
        style={{ background: "var(--surface-2)" }}
      >
        <div
          className="h-full rounded-full"
          style={{ width: `${v}%`, background: color }}
        />
      </div>
      <span
        className="w-7 text-right font-mono text-xs font-semibold tabular-nums"
        style={{ color: valueColor }}
      >
        {v}
      </span>
    </div>
  );
}

function ChipRow({
  label,
  color,
  items,
  variant,
}: {
  label: string;
  color: string;
  items: string[];
  variant: "good" | "warn";
}) {
  const bg = variant === "good" ? "var(--good-bg)" : "var(--warn-bg)";
  const border = variant === "good" ? "var(--good-border)" : "var(--warn-border)";
  return (
    <div className="mt-1">
      <div
        className="mb-2 font-mono text-[11px] font-semibold uppercase tracking-wide"
        style={{ color }}
      >
        {label}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {items.map((t, i) => (
          <span
            key={i}
            className="rounded-[7px] border px-2.5 py-1 text-xs"
            style={{ background: bg, borderColor: border, color }}
          >
            {t}
          </span>
        ))}
      </div>
    </div>
  );
}

function EmptyState({
  minScore,
  onLower,
}: {
  minScore: number;
  onLower: () => void;
}) {
  return (
    <div
      className="flex h-[340px] items-center justify-center rounded-xl border"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <div className="px-10 text-center">
        <div
          className="mx-auto flex h-[52px] w-[52px] items-center justify-center rounded-full border text-2xl"
          style={{
            background: "var(--good-bg)",
            borderColor: "var(--good-border)",
            color: "var(--good)",
          }}
        >
          ✓
        </div>
        <h3 className="mt-[18px] text-lg font-semibold" style={{ color: "var(--text)" }}>
          {"You're all caught up"}
        </h3>
        <p className="mt-2 text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
          No jobs above your minimum score of{" "}
          <span className="font-mono" style={{ color: "var(--text)" }}>
            {minScore}
          </span>
          . Lower the threshold to see more.
        </p>
        <button
          onClick={onLower}
          className="mt-[18px] h-[38px] rounded-[9px] border px-4 text-[13px] font-semibold"
          style={{
            background: "var(--surface)",
            borderColor: "var(--border)",
            color: "var(--label)",
          }}
        >
          Lower threshold
        </button>
      </div>
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div
      className="rounded-[14px] border p-5"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-[11px]">
          <div className="h-[34px] w-[34px] rounded-lg h-pulse" style={{ background: "var(--skeleton)" }} />
          <div>
            <div className="h-[15px] w-[200px] rounded h-pulse" style={{ background: "var(--skeleton)" }} />
            <div className="mt-2 h-[11px] w-[90px] rounded h-pulse" style={{ background: "var(--skeleton-2)" }} />
          </div>
        </div>
        <div className="h-[30px] w-[44px] rounded h-pulse" style={{ background: "var(--skeleton)" }} />
      </div>
      <div className="mt-[18px] flex flex-col gap-2.5">
        <div className="h-[11px] w-full rounded h-pulse" style={{ background: "var(--skeleton-2)" }} />
        <div className="h-[11px] w-full rounded h-pulse" style={{ background: "var(--skeleton-2)" }} />
        <div className="h-[11px] w-[65%] rounded h-pulse" style={{ background: "var(--skeleton-2)" }} />
      </div>
    </div>
  );
}
