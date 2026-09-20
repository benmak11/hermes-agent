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
  reconcileStats,
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
import { barColorWarm, initial, recPillWarm, scoreColorWarm } from "@/lib/ui";
import { TopNav } from "@/components/TopNav";
import { CompanyTile, tileHue } from "@/components/warm/CompanyTile";
import { Pill } from "@/components/warm/Pill";
import { GROUND, SERIF } from "@/components/warm/styles";

type PendingResponse = {
  jobs: Job[];
  /** Pending jobs before any filtering — 0 means nothing has been discovered. */
  pending_total?: number;
  /** Pending jobs that have been scored, at any score. */
  scored_total?: number;
};

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
        old ? { ...old, jobs: old.jobs.filter((j) => j.id !== job.id) } : old,
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
          ? {
              ...old,
              jobs: [p.job, ...old.jobs.filter((j) => j.id !== p.job.id)],
            }
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

  // Counts live in localStorage and survive a server-side wipe, which would
  // otherwise leave the header reporting a review session for jobs that no
  // longer exist. Runs before the counts are read below.
  const dataEpoch = profileData?.profile?.data_epoch ?? null;
  useEffect(() => {
    if (uid) reconcileStats(uid, dataEpoch);
  }, [uid, dataEpoch]);

  if (loading || !user || profileLoading || needsOnboarding) {
    return (
      <div className="wm" style={GROUND}>
        <div className="p-8" style={{ color: "var(--ink-4)" }}>
          Loading…
        </div>
      </div>
    );
  }

  const reviewed = reviewedCount(stats);
  const remaining = jobs.length;
  const total = reviewed + remaining;
  const pace = paceMinutes(stats, remaining);

  return (
    <div className="wm" style={GROUND}>
      <TopNav
        section="review"
        center={
          reviewed > 0 && total > 0 ? (
            <SessionProgress reviewed={reviewed} total={total} />
          ) : undefined
        }
        pill={firstRun ? <DiscoveryPill /> : undefined}
      />
      <main className="mx-auto w-full max-w-[920px] flex-1 px-7 py-7">
        <div className="mb-6 flex items-start justify-between gap-5">
          <div>
            <div className="flex items-center gap-2.5">
              <h1
                className="text-[28px] font-normal leading-[1.15]"
                style={{ fontFamily: SERIF, color: "var(--ink)" }}
              >
                {firstRun ? "Your first matches" : "Jobs to review"}
              </h1>
              {!firstRun && (
                <span
                  className="inline-flex h-[22px] min-w-6 items-center justify-center rounded-full px-[7px] text-[12px] font-bold"
                  style={{ background: "var(--ink)", color: "#fff9f2" }}
                >
                  {jobs.length}
                </span>
              )}
            </div>
            {firstRun ? (
              <div className="mt-2 text-[13px]" style={{ color: "var(--ink-4)" }}>
                <span style={{ color: "var(--sage)", fontWeight: 700 }}>
                  {jobs.length} {jobs.length === 1 ? "match" : "matches"}
                </span>{" "}
                so far — start reviewing, more will appear below
              </div>
            ) : (
              <div
                className="mt-2 flex items-center gap-1.5 text-[12px]"
                style={{ color: "var(--ink-4)" }}
              >
                <Kbd>a</Kbd> approve <Kbd>s</Kbd> skip <Kbd>r</Kbd> star{" "}
                <Kbd>z</Kbd> undo
              </div>
            )}
          </div>

          <label
            className="flex h-[38px] items-center gap-2.5 rounded-[11px] border px-3.5"
            style={{ background: "var(--surface-warm)", borderColor: "#e8dacb" }}
          >
            <span
              className="whitespace-nowrap text-[12px] font-semibold"
              style={{ color: "var(--ink-4)" }}
            >
              Min score
            </span>
            <input
              type="range"
              min={0}
              max={100}
              value={minScore}
              onChange={(e) => saveMinScore(Number(e.target.value))}
              className="w-[130px] accent-[var(--terracotta)]"
            />
            <span
              className="w-5 text-right text-[13px] font-bold tabular-nums"
              style={{ color: "var(--ink)" }}
            >
              {minScore}
            </span>
          </label>
        </div>

        {isLoading && <LoadingSkeleton />}
        {error && (
          <p className="text-[13.5px]" style={{ color: "var(--brick)" }}>
            Failed to load: {String(error)}
          </p>
        )}
        {!isLoading && jobs.length === 0 && !firstRun && (
          <EmptyState
            minScore={minScore}
            pendingTotal={data?.pending_total ?? null}
            scoredTotal={data?.scored_total ?? null}
            onLower={() => saveMinScore(0)}
          />
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
            className="mt-[18px] flex items-center gap-[18px] text-[12.5px] font-semibold"
            style={{ color: "var(--ink-4)" }}
          >
            <CountDot
              color="var(--sage)"
              label={`${stats.approved} approved`}
              href="/app/tracking"
            />
            <CountDot
              color="#b0a08d"
              label={`${stats.skipped} passed`}
              href="/app/tracking?tab=skipped"
            />
            <CountDot
              color="var(--honey)"
              label={`${stats.starred} saved`}
              href="/app/tracking?tab=starred"
            />
            <span className="ml-auto font-medium" style={{ color: "#96826f" }}>
              {remaining} left
              {pace != null ? ` · ~${pace} min at your pace` : ""}
              <button onClick={undo} className="wm-link ml-[7px] font-semibold">
                Undo last
              </button>
            </span>
          </div>
        )}
      </main>

      {pending && <UndoToast pending={pending} onUndo={undo} />}
    </div>
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
    <div className="flex items-center gap-3">
      <div
        className="h-[7px] w-[160px] overflow-hidden rounded-full"
        style={{ background: "#f0e3d3" }}
      >
        <div
          className="h-full rounded-full"
          style={{ width: `${pct}%`, background: "#7d9b6f" }}
        />
      </div>
      <span className="text-[12.5px] font-semibold" style={{ color: "var(--ink-3)" }}>
        {reviewed} of {total} reviewed
      </span>
    </div>
  );
}

function DiscoveryPill() {
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border px-[14px] py-[7px] text-[12px] font-bold"
      style={{
        background: "var(--honey-tint)",
        borderColor: "#f4dfb4",
        color: "#9a6216",
      }}
    >
      <span
        className="inline-block h-[11px] w-[11px] rounded-full border-2"
        style={{
          borderColor: "#9a6216",
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
      className="mt-4 rounded-[18px] px-[22px] py-5"
      style={{
        border: "1px dashed #d9c4a8",
        background: "rgba(255,252,248,0.6)",
      }}
    >
      <div className="flex items-center gap-[11px]">
        <div
          className="h-[34px] w-[34px] rounded-lg h-pulse"
          style={{ background: "#f0e3d3" }}
        />
        <div className="flex-1">
          <div
            className="h-[13px] w-[220px] rounded h-pulse"
            style={{ background: "#f0e3d3" }}
          />
          <div
            className="mt-[7px] h-[11px] w-[110px] rounded h-pulse"
            style={{ background: "#f6ede1" }}
          />
        </div>
        <span className="text-[11.5px] font-semibold" style={{ color: "#a3927f" }}>
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
      <Link href={href} className="wm-muted-link inline-flex items-center gap-1.5">
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
      className="h-slideup fixed bottom-[22px] left-1/2 z-20 flex -translate-x-1/2 items-center gap-3.5 rounded-[14px] py-[11px] pl-4 pr-3"
      style={{
        background: "var(--ink)",
        boxShadow: "0 10px 30px rgba(46,33,25,0.35)",
        animationDuration: "0.3s",
      }}
    >
      <span className="text-[13px]" style={{ color: "#e8dacb" }}>
        {DECISION_VERB[pending.decision]}{" "}
        <b style={{ color: "#fff9f2" }}>{pending.job.title}</b> ·{" "}
        {pending.job.company}
      </span>
      <button
        onClick={onUndo}
        className="wm-toast-btn inline-flex h-[30px] items-center gap-1.5 rounded-[9px] border px-3 text-[12.5px] font-semibold"
        style={{ borderColor: "var(--ink-3)", color: "#fff9f2" }}
      >
        Undo{" "}
        <kbd
          className="inline-flex h-[17px] min-w-4 items-center justify-center rounded border px-1 text-[10px] font-bold"
          style={{
            background: "var(--ink-3)",
            borderColor: "var(--ink-4)",
            color: "#e8dacb",
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
          stroke="var(--ink-3)"
          strokeWidth="2.5"
        />
        <circle
          cx="10"
          cy="10"
          r="8"
          fill="none"
          stroke="#d9873f"
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
      className="inline-flex h-[19px] min-w-[18px] items-center justify-center rounded-[5px] border px-[5px] text-[11px] font-bold"
      style={{
        background: "#f6ede1",
        borderColor: "#e8dacb",
        color: "var(--ink-3)",
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
  const pill = recPillWarm(m.recommendation);
  const dealHits = Math.max(0, Math.round(100 - m.breakdown.deal_breaker_penalty));

  return (
    <article
      className="h-slideup rounded-[20px] border p-6"
      style={{
        background: isTop ? "#fdf7ee" : "var(--surface-warm)",
        borderColor: isTop ? "#e0c8b6" : "var(--border-warm-hair)",
        boxShadow: isTop ? "0 0 0 4px rgba(184,83,47,0.07)" : "none",
      }}
    >
      <div className="flex items-start justify-between gap-5">
        <div className="min-w-0">
          <div className="flex items-center gap-[11px]">
            <CompanyTile initial={initial(job.company)} hue={tileHue(job.company)} />
            <div className="min-w-0">
              <h2
                className="truncate text-[16px] font-bold"
                style={{ color: "var(--ink)" }}
              >
                {job.title}
              </h2>
              <div className="text-[13px]" style={{ color: "var(--ink-4)" }}>
                {job.company}
              </div>
            </div>
          </div>
          <div
            className="mt-3 flex flex-wrap items-center gap-2 text-[12.5px] font-medium"
            style={{ color: "var(--ink-4)" }}
          >
            <span>{job.location ?? "—"}</span>
            <Dot />
            <span>{job.source}</span>
            <Dot />
            <a
              href={job.url}
              target="_blank"
              rel="noreferrer"
              className="wm-link font-semibold"
            >
              View posting ↗
            </a>
            {job.discovered_via === "unvetted" && (
              <Pill tone="warn">★ First-time company</Pill>
            )}
          </div>
        </div>

        <div className="flex-shrink-0 text-right">
          <div className="flex items-baseline justify-end gap-0.5">
            <span
              className="tabular-nums"
              style={{
                fontFamily: SERIF,
                fontSize: 44,
                lineHeight: 1,
                color: scoreColorWarm(m.recommendation),
              }}
            >
              {Math.round(m.overall_score)}
            </span>
            <span className="text-[13px]" style={{ color: "#a3927f" }}>
              /100 match
            </span>
          </div>
          <div className="mt-2.5">
            <Pill tone={pill.tone}>● {pill.label}</Pill>
          </div>
        </div>
      </div>

      <p
        className="mt-4 text-[13.5px] leading-[1.6]"
        style={{ color: "var(--ink-3)" }}
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
            fill="var(--brick)"
            goodWhenZero
          />
          {m.matched_strengths.length > 0 && (
            <ChipRow label="Strengths" color="var(--sage)" items={m.matched_strengths} variant="good" />
          )}
          {m.gaps.length > 0 && (
            <ChipRow label="Gaps" color="#9a6216" items={m.gaps} variant="warn" />
          )}
          {m.red_flags_hit.length > 0 && (
            <ChipRow label="Red flags" color="var(--brick)" items={m.red_flags_hit} variant="bad" />
          )}
        </div>
      )}

      <div className="my-[18px] h-px" style={{ background: "#f0e3d3" }} />

      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <button
            onClick={() => onDecide("approved")}
            className="wm-cta inline-flex h-[38px] items-center gap-2 rounded-[11px] px-[18px] text-[13.5px] font-semibold"
          >
            ✓ Approve
          </button>
          <GhostBtn onClick={() => onDecide("rejected")}>→ Skip</GhostBtn>
          <GhostBtn onClick={() => onDecide("starred")}>★ Star</GhostBtn>
        </div>
        <button
          onClick={() => setOpen((o) => !o)}
          className={
            open
              ? "wm-toggle wm-toggle-open inline-flex items-center gap-1.5 text-[13px] font-medium"
              : "wm-toggle inline-flex items-center gap-1.5 text-[13px] font-medium"
          }
        >
          {open ? "▾" : "▸"} Breakdown
        </button>
      </div>
    </article>
  );
}

function Dot() {
  return <span style={{ color: "#d9c4a8" }}>·</span>;
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
      className="wm-ghost inline-flex h-[38px] items-center gap-1.5 rounded-[11px] border px-4 text-[13.5px] font-semibold"
      style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
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
  const color = fill ?? barColorWarm(v);
  const valueColor = goodWhenZero && v === 0 ? "var(--sage)" : "var(--ink)";
  return (
    <div className="flex items-center gap-3.5">
      <span
        className="w-[118px] text-[12px] font-semibold"
        style={{ color: "var(--ink-3)" }}
      >
        {label}
      </span>
      <div
        className="h-[7px] flex-1 overflow-hidden rounded-full"
        style={{ background: "#f0e3d3" }}
      >
        <div
          className="h-full rounded-full"
          style={{ width: `${v}%`, background: color }}
        />
      </div>
      <span
        className="w-7 text-right text-[12px] font-bold tabular-nums"
        style={{ color: valueColor }}
      >
        {v}
      </span>
    </div>
  );
}

type ChipVariant = "good" | "warn" | "bad";

const CHIP: Record<ChipVariant, { bg: string; border: string }> = {
  good: { bg: "var(--sage-tint)", border: "#cfe0c8" },
  warn: { bg: "var(--honey-tint)", border: "#f4dfb4" },
  bad: { bg: "#fdeeea", border: "#f2cfc3" },
};

function ChipRow({
  label,
  color,
  items,
  variant,
}: {
  label: string;
  color: string;
  items: string[];
  variant: ChipVariant;
}) {
  const { bg, border } = CHIP[variant];
  return (
    <div className="mt-1">
      <div
        className="mb-2 text-[11px] font-bold uppercase tracking-[0.08em]"
        style={{ color }}
      >
        {label}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {items.map((t, i) => (
          <span
            key={i}
            className="rounded-[8px] border px-2.5 py-1 text-[12px]"
            style={{ background: bg, borderColor: border, color }}
          >
            {t}
          </span>
        ))}
      </div>
    </div>
  );
}

/**
 * An empty queue has three causes and only one of them is the threshold.
 * Offering "Lower threshold" to an account with no jobs at all — a new invite,
 * or one whose data was wiped — sends them to a control that cannot help,
 * which is what it did before these counts existed.
 */
function EmptyState({
  minScore,
  pendingTotal,
  scoredTotal,
  onLower,
}: {
  minScore: number;
  pendingTotal: number | null;
  scoredTotal: number | null;
  onLower: () => void;
}) {
  // Null counts mean an older API that doesn't report them; fall back to the
  // threshold copy rather than inventing a state we can't actually observe.
  const nothingDiscovered = pendingTotal === 0;
  const nothingScoredYet =
    pendingTotal !== null &&
    pendingTotal > 0 &&
    scoredTotal !== null &&
    scoredTotal === 0;

  const { icon, tone, heading, body, action } = nothingDiscovered
    ? {
        icon: "◔",
        tone: "muted" as const,
        heading: "No jobs yet",
        body: "Nothing has been discovered for this account yet. Discovery runs on a schedule, or you can start one from Companies.",
        action: null,
      }
    : nothingScoredYet
      ? {
          icon: "◔",
          tone: "muted" as const,
          heading: "Scoring in progress",
          body: `${pendingTotal} job${pendingTotal === 1 ? "" : "s"} discovered and waiting to be scored. They'll appear here as the matcher works through them.`,
          action: null,
        }
      : {
          icon: "✓",
          tone: "good" as const,
          heading: "You're all caught up",
          body: null,
          action: "lower" as const,
        };

  return (
    <div
      className="flex h-[340px] items-center justify-center rounded-[20px] border"
      style={{
        background: "var(--surface-warm)",
        borderColor: "var(--border-warm-hair)",
      }}
    >
      <div className="px-10 text-center">
        <div
          className="mx-auto flex h-[52px] w-[52px] items-center justify-center rounded-full border text-2xl"
          style={{
            background: tone === "good" ? "var(--sage-tint)" : "#f6ede1",
            borderColor: tone === "good" ? "#cfe0c8" : "#e8dacb",
            color: tone === "good" ? "var(--sage)" : "var(--ink-4)",
          }}
        >
          {icon}
        </div>
        <h3 className="mt-[18px] text-[18px] font-semibold" style={{ color: "var(--ink)" }}>
          {heading}
        </h3>
        <p className="mt-2 text-[14px] leading-relaxed" style={{ color: "var(--ink-4)" }}>
          {body ?? (
            <>
              No jobs above your minimum score of{" "}
              <span className="font-semibold" style={{ color: "var(--ink)" }}>
                {minScore}
              </span>
              . Lower the threshold to see more.
            </>
          )}
        </p>
        {action === "lower" && (
          <button
            onClick={onLower}
            className="wm-ghost mt-[18px] h-[38px] rounded-[11px] border px-4 text-[13px] font-semibold"
            style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
          >
            Lower threshold
          </button>
        )}
      </div>
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <div
      className="rounded-[18px] border p-5"
      style={{
        background: "var(--surface-warm)",
        borderColor: "var(--border-warm-hair)",
      }}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-[11px]">
          <div className="h-[34px] w-[34px] rounded-lg h-pulse" style={{ background: "#f0e3d3" }} />
          <div>
            <div className="h-[15px] w-[200px] rounded h-pulse" style={{ background: "#f0e3d3" }} />
            <div className="mt-2 h-[11px] w-[90px] rounded h-pulse" style={{ background: "#f6ede1" }} />
          </div>
        </div>
        <div className="h-[30px] w-[44px] rounded h-pulse" style={{ background: "#f0e3d3" }} />
      </div>
      <div className="mt-[18px] flex flex-col gap-2.5">
        <div className="h-[11px] w-full rounded h-pulse" style={{ background: "#f6ede1" }} />
        <div className="h-[11px] w-full rounded h-pulse" style={{ background: "#f6ede1" }} />
        <div className="h-[11px] w-[65%] rounded h-pulse" style={{ background: "#f6ede1" }} />
      </div>
    </div>
  );
}
