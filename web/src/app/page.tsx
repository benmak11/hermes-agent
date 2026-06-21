// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Decision, Job } from "@/lib/types";
import { avatarColor, barColor, initial, recPill, scoreColor } from "@/lib/ui";
import { TopNav } from "@/components/TopNav";

type PendingResponse = { jobs: Job[] };

export default function VettingPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [minScore, setMinScore] = useState(60);

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

  const queryKey = ["pending", minScore] as const;

  const { data, isLoading, error } = useQuery({
    queryKey,
    queryFn: () =>
      apiFetch<PendingResponse>(`/jobs/pending?min_score=${minScore}`),
    enabled: !!user,
  });

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: Decision }) =>
      apiFetch(`/jobs/${id}/decide`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      }),
    onMutate: async ({ id }) => {
      await queryClient.cancelQueries({ queryKey });
      const prev = queryClient.getQueryData<PendingResponse>(queryKey);
      queryClient.setQueryData<PendingResponse>(queryKey, (old) =>
        old ? { jobs: old.jobs.filter((j) => j.id !== id) } : old,
      );
      return { prev };
    },
    onError: (_e, _vars, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(queryKey, ctx.prev);
    },
  });

  const jobs = data?.jobs ?? [];
  const top = jobs[0];

  const act = useCallback(
    (decision: Decision) => {
      if (top) decide.mutate({ id: top.id, decision });
    },
    [top, decide],
  );

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLElement && e.target.tagName === "INPUT") return;
      if (e.key === "a") act("approved");
      else if (e.key === "s") act("rejected");
      else if (e.key === "r") act("starred");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [act]);

  if (loading || !user) {
    return <div className="p-8" style={{ color: "var(--muted)" }}>Loading…</div>;
  }

  return (
    <>
      <TopNav section="review" />
      <main className="mx-auto w-full max-w-[760px] flex-1 px-8 py-7">
        <div className="mb-6 flex items-start justify-between gap-5">
          <div>
            <div className="flex items-center gap-2.5">
              <h1
                className="text-[22px] font-semibold tracking-tight"
                style={{ color: "var(--text)" }}
              >
                Jobs to review
              </h1>
              <span
                className="inline-flex h-[22px] min-w-6 items-center justify-center rounded-full px-[7px] font-mono text-xs font-semibold"
                style={{ background: "var(--text)", color: "var(--surface)" }}
              >
                {jobs.length}
              </span>
            </div>
            <div
              className="mt-2 flex items-center gap-1.5 text-xs"
              style={{ color: "var(--muted)" }}
            >
              <Kbd>a</Kbd> approve <Kbd>s</Kbd> skip <Kbd>r</Kbd> star
            </div>
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
              onChange={(e) => setMinScore(Number(e.target.value))}
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
        {!isLoading && jobs.length === 0 && (
          <EmptyState minScore={minScore} onLower={() => setMinScore(0)} />
        )}

        <div className="space-y-4">
          {jobs.map((job, i) => (
            <JobCard
              key={job.id}
              job={job}
              isTop={i === 0}
              onDecide={(d) => decide.mutate({ id: job.id, decision: d })}
            />
          ))}
        </div>
      </main>
    </>
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
      className="rounded-[14px] border p-[22px]"
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
