// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Application, DecideValue, Job } from "@/lib/types";
import { avatarColor, initial, recPill, scoreColor } from "@/lib/ui";
import { TopNav } from "@/components/TopNav";

type AppsResponse = { applications: Application[] };
type JobsResponse = { jobs: Job[] };
type Tab = "pipeline" | "starred" | "skipped";

/** How recently an application must have been created to get the arrival tint. */
const JUST_APPROVED_MS = 2 * 60 * 1000;

export default function TrackingPage() {
  // useSearchParams needs a Suspense boundary for prerendering.
  return (
    <Suspense fallback={<div className="p-8" style={{ color: "var(--muted)" }}>Loading…</div>}>
      <TrackingInner />
    </Suspense>
  );
}

function TrackingInner() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();
  const params = useSearchParams();
  const initialTab = params.get("tab");
  const [tab, setTab] = useState<Tab>(
    initialTab === "starred" || initialTab === "skipped" ? initialTab : "pipeline",
  );

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

  const { data: appsData, isLoading } = useQuery({
    queryKey: ["applications"],
    queryFn: () => apiFetch<AppsResponse>("/applications"),
    enabled: !!user,
    // The agents write status as they go — poll so strips fill in place.
    refetchInterval: 5000,
  });

  const { data: starredData } = useQuery({
    queryKey: ["decided", "starred"],
    queryFn: () => apiFetch<JobsResponse>("/jobs/decided?decision=starred"),
    enabled: !!user,
  });
  const { data: skippedData } = useQuery({
    queryKey: ["decided", "rejected"],
    queryFn: () => apiFetch<JobsResponse>("/jobs/decided?decision=rejected"),
    enabled: !!user,
  });

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: DecideValue }) =>
      apiFetch(`/jobs/${id}/decide`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["decided"] });
      queryClient.invalidateQueries({ queryKey: ["applications"] });
      queryClient.invalidateQueries({ queryKey: ["pending"] });
    },
  });

  if (loading || !user) {
    return <div className="p-8" style={{ color: "var(--muted)" }}>Loading…</div>;
  }

  const apps = appsData?.applications ?? [];
  const starred = starredData?.jobs ?? [];
  const skipped = skippedData?.jobs ?? [];

  const counts = {
    tailoring: apps.filter((a) =>
      ["queued", "tailoring", "ready_for_review"].includes(a.status),
    ).length,
    applied: apps.filter((a) => ["submitting", "submitted"].includes(a.status))
      .length,
    responses: apps.filter((a) => a.status === "responded").length,
  };

  return (
    <>
      <TopNav section="tracking" />
      <main className="mx-auto w-full max-w-[820px] flex-1 px-8 py-7">
        <div className="mb-5 flex items-end justify-between gap-5">
          <div>
            <h1
              className="text-[22px] font-semibold tracking-tight"
              style={{ color: "var(--text)" }}
            >
              Applications
            </h1>
            <div
              className="mt-2 font-mono text-[12.5px] font-medium"
              style={{ color: "var(--muted)" }}
            >
              <b style={{ color: "var(--text)" }}>{apps.length}</b> in pipeline ·{" "}
              <b style={{ color: "var(--accent)" }}>{counts.tailoring}</b> tailoring ·{" "}
              <b style={{ color: "var(--good)" }}>{counts.applied}</b> applied ·{" "}
              <b style={{ color: "var(--warn)" }}>{counts.responses}</b>{" "}
              {counts.responses === 1 ? "response" : "responses"}
            </div>
          </div>
          <div
            className="flex items-center gap-2 font-mono text-xs font-medium"
            style={{ color: "var(--subtle)" }}
          >
            <LegendSquare color="var(--border-mid)" label="approved" />
            <LegendSquare color="var(--accent)" label="tailoring" />
            <LegendSquare color="var(--good)" label="applied" />
            <LegendSquare color="var(--star)" label="response" />
          </div>
        </div>

        <div
          className="mb-[18px] inline-flex gap-0.5 rounded-[10px] p-[3px]"
          style={{ background: "var(--surface-2)" }}
        >
          <TabBtn active={tab === "pipeline"} onClick={() => setTab("pipeline")} label="Pipeline" count={apps.length} />
          <TabBtn active={tab === "starred"} onClick={() => setTab("starred")} label="★ Starred" count={starred.length} />
          <TabBtn active={tab === "skipped"} onClick={() => setTab("skipped")} label="Skipped" count={skipped.length} />
        </div>

        {tab === "pipeline" && (
          <>
            {isLoading && <p style={{ color: "var(--muted)" }}>Loading…</p>}
            {!isLoading && apps.length === 0 && (
              <EmptyCard>
                Nothing in the pipeline yet. Approve a job in{" "}
                <Link href="/" className="font-semibold" style={{ color: "var(--accent)" }}>
                  Review
                </Link>{" "}
                and the agent takes it from there.
              </EmptyCard>
            )}
            <div className="space-y-3">
              {apps.map((app) => (
                <PipelineRow key={app.id} app={app} />
              ))}
            </div>
            {apps.length > 0 && (
              <p
                className="mt-5 text-center font-mono text-[11px] font-medium"
                style={{ color: "var(--subtle)" }}
              >
                approved → tailoring → applied → response · status written by the
                tailoring + application agents
              </p>
            )}
          </>
        )}

        {tab === "starred" && (
          <DecidedList
            jobs={starred}
            empty="No starred jobs. Star a job in Review (r) to shelve it for later."
            actions={(job) => (
              <>
                <RowBtn
                  primary
                  onClick={() => decide.mutate({ id: job.id, decision: "approved" })}
                >
                  ✓ Approve
                </RowBtn>
                <RowBtn
                  onClick={() => decide.mutate({ id: job.id, decision: "pending" })}
                >
                  Back to queue
                </RowBtn>
              </>
            )}
          />
        )}

        {tab === "skipped" && (
          <DecidedList
            jobs={skipped}
            empty="No skipped jobs — everything you passed on would show up here."
            actions={(job) => (
              <>
                <RowBtn
                  onClick={() => decide.mutate({ id: job.id, decision: "pending" })}
                >
                  Restore to queue
                </RowBtn>
                <RowBtn
                  primary
                  onClick={() => decide.mutate({ id: job.id, decision: "approved" })}
                >
                  ✓ Approve
                </RowBtn>
              </>
            )}
          />
        )}
      </main>
    </>
  );
}

/* ---------- pipeline (mock 04) ---------- */

type Segment = { color: string; pulse?: boolean };

type PipelineView = {
  segments: Segment[];
  label: string;
  labelColor: string;
  /** When set, the strip label links to the application review page. */
  labelHref?: string;
  pill?: { text: string; bg: string; border: string; color: string };
  /** Card-level tint (arrival / response states). */
  card?: { bg: string; border: string };
  rightNote?: { text: string; color: string };
};

const IDLE = "var(--surface-2)";

function relDays(iso: string | null | undefined): string {
  if (!iso) return "";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);
  if (days <= 0) return "today";
  return `${days}d ago`;
}

function pipelineView(app: Application): PipelineView {
  const good = "var(--good)";
  const accent = "var(--accent)";
  const createdAt = app.timeline?.[0]?.at;
  const justApproved =
    createdAt && Date.now() - new Date(createdAt).getTime() < JUST_APPROVED_MS;
  const submittedAt =
    app.confirmation?.submitted_at ?? app.last_submitted_at ?? null;

  switch (app.status) {
    case "queued":
    case "tailoring":
      return {
        segments: [
          { color: good },
          { color: accent, pulse: true },
          { color: IDLE },
          { color: IDLE },
        ],
        label: "tailoring resume…",
        labelColor: accent,
        ...(justApproved
          ? {
              card: { bg: "var(--offer-bg)", border: "var(--good-border)" },
              rightNote: { text: "just approved ✓", color: good },
            }
          : {}),
      };
    case "ready_for_review":
      return {
        segments: [{ color: good }, { color: good }, { color: IDLE }, { color: IDLE }],
        label: "review resume →",
        labelColor: accent,
        labelHref: `/applications/${app.id}/review`,
        pill: {
          text: "ready for review",
          bg: "var(--good-bg)",
          border: "var(--good-border)",
          color: good,
        },
      };
    case "submitting":
      return {
        segments: [
          { color: good },
          { color: good },
          { color: accent, pulse: true },
          { color: IDLE },
        ],
        label: "submitting…",
        labelColor: accent,
      };
    case "submitted":
      return {
        segments: [{ color: good }, { color: good }, { color: good }, { color: IDLE }],
        label: "awaiting response",
        labelColor: "var(--subtle)",
        pill: {
          text: `applied${submittedAt ? ` · ${relDays(submittedAt)}` : ""}`,
          bg: "var(--good-bg)",
          border: "var(--good-border)",
          color: good,
        },
      };
    case "responded":
      return {
        segments: [
          { color: good },
          { color: good },
          { color: good },
          { color: "var(--star)" },
        ],
        label: "response received",
        labelColor: "var(--warn)",
        pill: {
          text: "★ recruiter replied",
          bg: "var(--first-bg)",
          border: "var(--warn-border)",
          color: "var(--first-text)",
        },
        card: { bg: "var(--warn-bg)", border: "var(--warn-border)" },
      };
    case "posting_removed": {
      // The listing was taken down before we could submit — dismissed by the
      // agent, nothing to retry. Which segment died mirrors the failed case.
      const deadSeg = app.last_submitted_at ? 2 : 1;
      return {
        segments: [0, 1, 2, 3].map((i) => ({
          color: i < deadSeg ? good : i === deadSeg ? "var(--danger)" : IDLE,
        })),
        label: "listing taken down — dismissed",
        labelColor: "var(--muted)",
        pill: {
          text: "posting removed",
          bg: "var(--danger-bg)",
          border: "var(--danger-border)",
          color: "var(--danger)",
        },
      };
    }
    case "needs_input":
      // Filled as far as automation goes — the user finishes on the ATS form.
      return {
        segments: [
          { color: good },
          { color: good },
          { color: "var(--warn)", pulse: true },
          { color: IDLE },
        ],
        label: "finish a few questions →",
        labelColor: "var(--warn)",
        labelHref: `/applications/${app.id}/review`,
        pill: {
          text: "needs your input",
          bg: "var(--warn-bg)",
          border: "var(--warn-border)",
          color: "var(--warn)",
        },
      };
    case "failed":
    default: {
      // Failed after a submit attempt → the applied segment broke; otherwise
      // tailoring did.
      const failedSeg = app.last_submitted_at ? 2 : 1;
      return {
        segments: [0, 1, 2, 3].map((i) => ({
          color:
            i < failedSeg ? good : i === failedSeg ? "var(--danger)" : IDLE,
        })),
        label: "failed — open to retry →",
        labelColor: "var(--danger)",
        labelHref: `/applications/${app.id}/review`,
        pill: {
          text: "failed",
          bg: "var(--danger-bg)",
          border: "var(--danger-border)",
          color: "var(--danger)",
        },
      };
    }
  }
}

function PipelineRow({ app }: { app: Application }) {
  const company = app.job_company ?? "—";
  const av = avatarColor(company);
  const v = pipelineView(app);
  return (
    <div
      className="h-slideup rounded-xl border px-[18px] py-[15px]"
      style={{
        background: v.card?.bg ?? "var(--surface)",
        borderColor: v.card?.border ?? "var(--border)",
      }}
    >
      <div className="flex items-center gap-3">
        <span
          className="flex h-[30px] w-[30px] flex-none items-center justify-center rounded-[7px] text-[13px] font-bold"
          style={{ background: av.bg, color: av.color }}
        >
          {initial(company)}
        </span>
        <Link
          href={`/applications/${app.id}/review`}
          className="min-w-0 flex-1 truncate"
        >
          <span
            className="text-[14.5px] font-semibold"
            style={{ color: "var(--text)" }}
          >
            {app.job_title ?? app.job_id}
          </span>
          <span className="text-[13px]" style={{ color: "var(--muted)" }}>
            {" "}
            · {company}
          </span>
        </Link>
        {v.rightNote ? (
          <span
            className="flex-none font-mono text-[11px] font-semibold"
            style={{ color: v.rightNote.color }}
          >
            {v.rightNote.text}
          </span>
        ) : v.pill ? (
          <span
            className="inline-flex flex-none items-center gap-1.5 rounded-full border px-2.5 py-[3px] font-mono text-[11px] font-semibold"
            style={{
              background: v.pill.bg,
              borderColor: v.pill.border,
              color: v.pill.color,
            }}
          >
            {v.pill.text}
          </span>
        ) : null}
      </div>
      <div className="mt-3 flex items-center gap-1.5">
        <div className="flex flex-1 items-center gap-1.5">
          {v.segments.map((s, i) => (
            <span
              key={i}
              className={`h-1 flex-1 rounded-sm${s.pulse ? " h-pulse" : ""}`}
              style={{ background: s.color }}
            />
          ))}
        </div>
        {v.labelHref ? (
          <Link
            href={v.labelHref}
            className="font-mono text-[11.5px] font-medium"
            style={{ color: v.labelColor }}
          >
            {v.label}
          </Link>
        ) : (
          <span
            className="font-mono text-[11.5px] font-medium"
            style={{ color: v.labelColor }}
          >
            {v.label}
          </span>
        )}
      </div>
    </div>
  );
}

/* ---------- starred / skipped shelves ---------- */

function DecidedList({
  jobs,
  empty,
  actions,
}: {
  jobs: Job[];
  empty: string;
  actions: (job: Job) => React.ReactNode;
}) {
  if (jobs.length === 0) {
    return <EmptyCard>{empty}</EmptyCard>;
  }
  return (
    <div
      className="overflow-hidden rounded-xl border"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      {jobs.map((job) => {
        const av = avatarColor(job.company);
        const pill = recPill(job.match.recommendation);
        return (
          <div
            key={job.id}
            className="flex items-center gap-3 border-b px-4 py-3 last:border-0"
            style={{ borderColor: "var(--divider)" }}
          >
            <span
              className="flex h-[30px] w-[30px] flex-none items-center justify-center rounded-[7px] text-[13px] font-bold"
              style={{ background: av.bg, color: av.color }}
            >
              {initial(job.company)}
            </span>
            <div className="min-w-0 flex-1">
              <div className="truncate">
                <span
                  className="text-[14px] font-semibold"
                  style={{ color: "var(--text)" }}
                >
                  {job.title}
                </span>
                <span className="text-[13px]" style={{ color: "var(--muted)" }}>
                  {" "}
                  · {job.company}
                </span>
              </div>
              <div
                className="mt-0.5 flex items-center gap-2 font-mono text-[11px] font-medium"
                style={{ color: "var(--subtle)" }}
              >
                <span
                  className="font-semibold tabular-nums"
                  style={{ color: scoreColor(job.match.recommendation) }}
                >
                  {Math.round(job.match.overall_score)}
                </span>
                <span style={{ color: pill.color }}>{pill.label}</span>
                <a
                  href={job.url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-semibold"
                  style={{ color: "var(--accent)" }}
                >
                  View posting ↗
                </a>
              </div>
            </div>
            <div className="flex flex-none gap-2">{actions(job)}</div>
          </div>
        );
      })}
    </div>
  );
}

/* ---------- shared bits ---------- */

function LegendSquare({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-2 w-2 rounded-sm" style={{ background: color }} />
      {label}
    </span>
  );
}

function TabBtn({
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
      className="inline-flex items-center gap-[7px] rounded-[7px] px-[13px] py-1.5 text-[13px]"
      style={{
        background: active ? "var(--surface)" : "transparent",
        color: active ? "var(--text)" : "var(--label)",
        fontWeight: active ? 600 : 500,
        boxShadow: active ? "0 1px 2px rgba(0,0,0,0.06)" : "none",
      }}
    >
      {label}
      <span
        className="font-mono text-[11px] font-semibold"
        style={{ color: "var(--subtle)" }}
      >
        {count}
      </span>
    </button>
  );
}

function RowBtn({
  onClick,
  children,
  primary,
}: {
  onClick: () => void;
  children: React.ReactNode;
  primary?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className="h-[30px] rounded-[7px] px-[13px] text-xs"
      style={
        primary
          ? { background: "var(--text)", color: "var(--surface)", fontWeight: 600 }
          : {
              background: "var(--surface)",
              border: "1px solid var(--border)",
              color: "var(--label)",
              fontWeight: 500,
            }
      }
    >
      {children}
    </button>
  );
}

function EmptyCard({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="flex h-[220px] items-center justify-center rounded-xl border text-center"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <p
        className="max-w-[380px] px-8 text-sm leading-relaxed"
        style={{ color: "var(--muted)" }}
      >
        {children}
      </p>
    </div>
  );
}
