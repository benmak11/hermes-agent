// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Application, DecideValue, Job } from "@/lib/types";
import { initial, recPillWarm, scoreColorWarm } from "@/lib/ui";
import { TopNav } from "@/components/TopNav";
import { CompanyTile, tileHue } from "@/components/warm/CompanyTile";
import { GROUND, SERIF } from "@/components/warm/styles";
import { pipelineView } from "../applications/status";

type AppsResponse = { applications: Application[] };
type JobsResponse = { jobs: Job[] };
type Tab = "pipeline" | "starred" | "skipped";

export default function TrackingPage() {
  // useSearchParams needs a Suspense boundary for prerendering.
  return (
    <div className="wm" style={GROUND}>
      <Suspense fallback={<div className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>Loading…</div>}>
        <TrackingInner />
      </Suspense>
    </div>
  );
}

function TrackingInner() {
  const { user, loading } = useAuth();
  const queryClient = useQueryClient();
  const params = useSearchParams();
  const initialTab = params.get("tab");
  const [tab, setTab] = useState<Tab>(
    initialTab === "starred" || initialTab === "skipped" ? initialTab : "pipeline",
  );

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
    return <div className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>Loading…</div>;
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
      <main className="mx-auto w-full max-w-[920px] flex-1 px-7 py-7">
        <div className="flex items-end justify-between gap-5">
          <div>
            <h1
              className="text-[28px] font-normal leading-[1.15]"
              style={{ fontFamily: SERIF, color: "var(--ink)" }}
            >
              Applications
            </h1>
            <div className="mt-2 text-[13px]" style={{ color: "var(--ink-4)" }}>
              <b style={{ color: "var(--ink)" }}>{apps.length}</b> in progress ·{" "}
              <b style={{ color: "var(--terracotta)" }}>{counts.tailoring}</b> being written ·{" "}
              <b style={{ color: "var(--sage)" }}>{counts.applied}</b> sent ·{" "}
              <b style={{ color: "var(--honey)" }}>{counts.responses}</b>{" "}
              {counts.responses === 1 ? "reply" : "replies"}
            </div>
          </div>
          <div
            className="flex items-center gap-3 text-[11.5px] font-semibold"
            style={{ color: "#a3927f" }}
          >
            <LegendSquare color="#dfd0bd" label="you said yes" />
            <LegendSquare color="var(--terracotta)" label="being written" />
            <LegendSquare color="var(--sage)" label="sent" />
            <LegendSquare color="var(--honey)" label="reply" />
          </div>
        </div>

        <div
          className="mt-[18px] mb-4 inline-flex gap-[3px] rounded-[14px] p-1"
          style={{ background: "#f6ede1" }}
        >
          <TabBtn active={tab === "pipeline"} onClick={() => setTab("pipeline")} label="In progress" count={apps.length} />
          <TabBtn active={tab === "starred"} onClick={() => setTab("starred")} label="★ Saved" count={starred.length} />
          <TabBtn active={tab === "skipped"} onClick={() => setTab("skipped")} label="Not for me" count={skipped.length} />
        </div>

        {tab === "pipeline" && (
          <>
            {isLoading && <p className="text-[13.5px]" style={{ color: "var(--ink-4)" }}>Loading…</p>}
            {!isLoading && apps.length === 0 && (
              <EmptyCard>
                Nothing in progress yet. Say yes to a job in{" "}
                <Link href="/app" className="wm-link font-bold">
                  Review
                </Link>{" "}
                and we take it from there.
              </EmptyCard>
            )}
            <div className="space-y-3">
              {apps.map((app) => (
                <PipelineRow key={app.id} app={app} />
              ))}
            </div>
            {apps.length > 0 && (
              <p
                className="mt-5 text-center text-[11.5px] font-medium"
                style={{ color: "#a3927f" }}
              >
                You say yes → we write your resume → we send it → they reply.
                Each step updates here on its own.
              </p>
            )}
          </>
        )}

        {tab === "starred" && (
          <DecidedList
            jobs={starred}
            empty="Anything you save for later waits here. Approve it when you're ready, or send it back to Review."
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
                  Show me again later
                </RowBtn>
              </>
            )}
          />
        )}

        {tab === "skipped" && (
          <DecidedList
            jobs={skipped}
            empty="Anything you say no to waits here. Changed your mind? Bring it back any time."
            actions={(job) => (
              <>
                <RowBtn
                  onClick={() => decide.mutate({ id: job.id, decision: "pending" })}
                >
                  Bring it back
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

/* ---------- pipeline (design 10) ---------- */

function PipelineRow({ app }: { app: Application }) {
  const company = app.job_company ?? "—";
  const v = pipelineView(app);
  return (
    <div
      className="h-slideup rounded-[18px] border px-5 py-4"
      style={{
        background: v.card?.bg ?? "var(--surface-warm)",
        borderColor: v.card?.border ?? "var(--border-warm-hair)",
      }}
    >
      <div className="flex items-center gap-[13px]">
        <CompanyTile initial={initial(company)} hue={tileHue(company)} size="sm" />
        <Link
          href={`/app/applications/${app.id}/review`}
          className="min-w-0 flex-1 truncate"
        >
          <span
            className="text-[14.5px] font-semibold"
            style={{ color: "var(--ink)" }}
          >
            {app.job_title ?? app.job_id}
          </span>
          <span className="text-[14.5px]" style={{ color: "var(--ink-4)" }}>
            {" "}
            · {company}
          </span>
        </Link>
        {v.rightNote ? (
          <span
            className="flex-none text-[11.5px] font-bold"
            style={{ color: v.rightNote.color }}
          >
            {v.rightNote.text}
          </span>
        ) : v.pill ? (
          <span
            className="inline-flex flex-none items-center rounded-full border px-[11px] py-1 text-[11.5px] font-bold"
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
      <div className="mt-3.5 flex items-center gap-2">
        {v.segments.map((s, i) => (
          <span
            key={i}
            className={`h-[5px] flex-1 rounded-[2px]${s.pulse ? " h-pulse" : ""}`}
            style={{ background: s.color }}
          />
        ))}
        {v.labelHref ? (
          <Link
            href={v.labelHref}
            className="min-w-[118px] text-right text-[11.5px] font-semibold"
            style={{ color: v.labelColor }}
          >
            {v.label}
          </Link>
        ) : (
          <span
            className="min-w-[118px] text-right text-[11.5px] font-semibold"
            style={{ color: v.labelColor }}
          >
            {v.label}
          </span>
        )}
      </div>
    </div>
  );
}

/* ---------- saved / passed shelves (design 11) ---------- */

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
      className="overflow-hidden rounded-[18px] border"
      style={{ background: "var(--surface-warm)", borderColor: "var(--border-warm-hair)" }}
    >
      {jobs.map((job) => {
        const color = scoreColorWarm(job.match.recommendation);
        const pill = recPillWarm(job.match.recommendation);
        return (
          <div
            key={job.id}
            className="flex items-center gap-[13px] border-b px-[18px] py-[15px] last:border-0"
            style={{ borderColor: "#f0e3d3" }}
          >
            <CompanyTile initial={initial(job.company)} hue={tileHue(job.company)} size="sm" />
            <div className="min-w-0 flex-1">
              <div className="truncate">
                <span
                  className="text-[14px] font-semibold"
                  style={{ color: "var(--ink)" }}
                >
                  {job.title}
                </span>
                <span className="text-[14px]" style={{ color: "var(--ink-4)" }}>
                  {" "}
                  · {job.company}
                </span>
              </div>
              <div className="mt-[3px] flex items-center gap-2.5 text-[11.5px] font-semibold">
                <span style={{ color }}>
                  {Math.round(job.match.overall_score)}/100 match
                </span>
                <span style={{ color }}>{pill.label}</span>
                <a
                  href={job.url}
                  target="_blank"
                  rel="noreferrer"
                  className="wm-link"
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
      <span className="h-2 w-2 rounded-[2px]" style={{ background: color }} />
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
      className={
        primary
          ? "wm-cta h-[34px] rounded-[11px] px-[14px] text-[12.5px] font-semibold"
          : "wm-ghost h-[34px] rounded-[11px] border px-[14px] text-[12.5px] font-semibold"
      }
      style={primary ? undefined : { borderColor: "#e8dacb", color: "var(--ink-2)" }}
    >
      {children}
    </button>
  );
}

function EmptyCard({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="rounded-[18px] border border-dashed px-[22px] py-5 text-[13.5px] leading-[1.6]"
      style={{ background: "#fdf7ee", borderColor: "#d9c4a8", color: "var(--ink-4)" }}
    >
      {children}
    </div>
  );
}
