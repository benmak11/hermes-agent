// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Application } from "@/lib/types";
import { avatarColor, initial } from "@/lib/ui";
import { TopNav } from "@/components/TopNav";
import { statusPill } from "./status";

type ListResponse = { applications: Application[] };

export default function ApplicationsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

  const { data, isLoading, error } = useQuery({
    queryKey: ["applications"],
    queryFn: () => apiFetch<ListResponse>("/applications"),
    enabled: !!user,
    // Poll so freshly-approved jobs flip from "tailoring" to "ready" in place.
    refetchInterval: 5000,
  });

  if (loading || !user) {
    return (
      <div className="p-8" style={{ color: "var(--muted)" }}>
        Loading…
      </div>
    );
  }

  const apps = data?.applications ?? [];

  return (
    <>
      <TopNav section="applications" />
      <main className="mx-auto w-full max-w-[760px] flex-1 px-8 py-7">
        <h1
          className="mb-6 text-[22px] font-semibold tracking-tight"
          style={{ color: "var(--text)" }}
        >
          Applications
        </h1>

        {isLoading && (
          <p style={{ color: "var(--muted)" }}>Loading applications…</p>
        )}
        {error && (
          <p style={{ color: "var(--danger)" }}>
            Failed to load: {String(error)}
          </p>
        )}
        {!isLoading && apps.length === 0 && (
          <div
            className="flex h-[260px] items-center justify-center rounded-xl border text-center"
            style={{ background: "var(--surface)", borderColor: "var(--border)" }}
          >
            <p
              className="max-w-[360px] px-8 text-sm leading-relaxed"
              style={{ color: "var(--muted)" }}
            >
              No applications yet. Approve a job on the review board and a tailored
              resume will be generated here.
            </p>
          </div>
        )}

        <div className="space-y-3">
          {apps.map((app) => (
            <AppRow key={app.id} app={app} />
          ))}
        </div>
      </main>
    </>
  );
}

function AppRow({ app }: { app: Application }) {
  const company = app.job_company ?? "—";
  const av = avatarColor(company);
  const pill = statusPill(app.status);
  return (
    <Link
      href={`/applications/${app.id}/review`}
      className="flex items-center justify-between gap-4 rounded-[12px] border p-4"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <div className="flex min-w-0 items-center gap-3">
        <span
          className="flex h-[34px] w-[34px] flex-none items-center justify-center rounded-lg text-[15px] font-bold"
          style={{ background: av.bg, color: av.color }}
        >
          {initial(company)}
        </span>
        <div className="min-w-0">
          <div
            className="truncate text-[15px] font-semibold"
            style={{ color: "var(--text)" }}
          >
            {app.job_title ?? app.job_id}
          </div>
          <div className="text-[13px]" style={{ color: "var(--muted)" }}>
            {company}
          </div>
        </div>
      </div>
      <span
        className="inline-flex flex-none items-center gap-1.5 rounded-full border px-[9px] py-[3px] font-mono text-[11px] font-semibold"
        style={{ background: pill.bg, borderColor: pill.border, color: pill.color }}
      >
        <span
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ background: pill.color }}
        />
        {pill.label}
      </span>
    </Link>
  );
}
