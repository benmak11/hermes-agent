// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Application, RoleBullets } from "@/lib/types";
import { TopNav } from "@/components/TopNav";
import { statusPill } from "../../status";

export default function ReviewPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const id = params.id;
  const queryClient = useQueryClient();
  const queryKey = ["application", id] as const;

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

  const { data: app, isLoading, error } = useQuery({
    queryKey,
    queryFn: () => apiFetch<Application>(`/applications/${id}`),
    enabled: !!user,
    // Poll only while tailoring is in flight, then stop.
    refetchInterval: (q) =>
      q.state.data?.status === "tailoring" ? 3000 : false,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey });

  const saveObjective = useMutation({
    mutationFn: (objective_text: string) =>
      apiFetch(`/applications/${id}/objective`, {
        method: "PUT",
        body: JSON.stringify({ objective_text }),
      }),
    onSuccess: invalidate,
  });

  const regenerate = useMutation({
    mutationFn: () =>
      apiFetch(`/applications/${id}/regenerate`, { method: "POST" }),
    onSuccess: invalidate,
  });

  const submit = useMutation({
    mutationFn: () =>
      apiFetch(`/applications/${id}/submit`, { method: "POST" }),
    onSuccess: invalidate,
  });

  if (loading || !user || isLoading) {
    return (
      <>
        <TopNav section="applications" />
        <main className="p-8" style={{ color: "var(--muted)" }}>
          Loading…
        </main>
      </>
    );
  }

  if (error || !app) {
    return (
      <>
        <TopNav section="applications" />
        <main className="p-8" style={{ color: "var(--danger)" }}>
          Failed to load application: {String(error)}
        </main>
      </>
    );
  }

  const pill = statusPill(app.status);

  return (
    <>
      <TopNav section="applications" />
      <main className="mx-auto w-full max-w-[820px] flex-1 px-8 py-7">
        <div className="mb-6 flex items-start justify-between gap-5">
          <div>
            <h1
              className="text-[22px] font-semibold tracking-tight"
              style={{ color: "var(--text)" }}
            >
              {app.job_title ?? app.job_id}
            </h1>
            <div className="mt-1 text-[13px]" style={{ color: "var(--muted)" }}>
              {app.job_company ?? ""}
            </div>
          </div>
          <span
            className="inline-flex flex-none items-center gap-1.5 rounded-full border px-[10px] py-[4px] font-mono text-[11px] font-semibold"
            style={{
              background: pill.bg,
              borderColor: pill.border,
              color: pill.color,
            }}
          >
            <span
              className="inline-block h-1.5 w-1.5 rounded-full"
              style={{ background: pill.color }}
            />
            {pill.label}
          </span>
        </div>

        {app.status === "tailoring" && (
          <Banner>
            Tailoring in progress — generating your objective and resume variant.
            This page updates automatically.
          </Banner>
        )}
        {app.status === "failed" && (
          <Banner danger>
            Tailoring failed:{" "}
            {[...app.timeline].reverse().find((e) => e.status === "failed")
              ?.note ?? "unknown error"}
            . Try regenerating.
          </Banner>
        )}

        {app.status !== "tailoring" && (
          <>
            <ObjectiveEditor
              key={app.objective_text ?? ""}
              initial={app.objective_text ?? ""}
              saving={saveObjective.isPending}
              onSave={(t) => saveObjective.mutate(t)}
            />

            <Section title="Resume diff — master vs tailored">
              <p
                className="mb-4 text-[13px] leading-relaxed"
                style={{ color: "var(--muted)" }}
              >
                Bullets are reordered by relevance to this JD and pruned per role.
                Kept bullets show in their tailored order; dropped bullets are
                dimmed.
              </p>
              {app.tailored_bullets.map((t) => (
                <RoleDiff
                  key={`${t.company}-${t.role}`}
                  tailored={t}
                  master={app.master_bullets.find(
                    (m) => m.company === t.company && m.role === t.role,
                  )}
                />
              ))}
            </Section>

            {app.resume_variant_uri && (
              <p
                className="mt-2 break-all font-mono text-[11px]"
                style={{ color: "var(--subtle)" }}
              >
                Resume: {app.resume_variant_uri}
              </p>
            )}
          </>
        )}

        <div className="mt-8 flex items-center gap-3">
          <button
            onClick={() => submit.mutate()}
            disabled={app.status !== "ready_for_review" || submit.isPending}
            className="inline-flex h-[40px] items-center gap-2 rounded-[9px] px-5 text-[13px] font-semibold disabled:opacity-40"
            style={{ background: "var(--text)", color: "var(--surface)" }}
          >
            ✓ Approve &amp; Submit
          </button>
          <button
            onClick={() => regenerate.mutate()}
            disabled={app.status === "tailoring" || regenerate.isPending}
            className="inline-flex h-[40px] items-center gap-1.5 rounded-[9px] border px-4 text-[13px] font-semibold disabled:opacity-40"
            style={{
              background: "var(--surface)",
              borderColor: "var(--border)",
              color: "var(--label)",
            }}
          >
            ↻ Regenerate
          </button>
        </div>
      </main>
    </>
  );
}

function Banner({
  children,
  danger,
}: {
  children: React.ReactNode;
  danger?: boolean;
}) {
  return (
    <div
      className="mb-5 rounded-[10px] border px-4 py-3 text-[13px] leading-relaxed"
      style={{
        background: danger ? "var(--warn-bg)" : "var(--surface-2)",
        borderColor: danger ? "var(--warn-border)" : "var(--border)",
        color: danger ? "var(--danger)" : "var(--label)",
      }}
    >
      {children}
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className="mb-6 rounded-[14px] border p-[22px]"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <h2
        className="mb-4 text-[15px] font-semibold tracking-tight"
        style={{ color: "var(--text)" }}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

function ObjectiveEditor({
  initial,
  saving,
  onSave,
}: {
  initial: string;
  saving: boolean;
  onSave: (text: string) => void;
}) {
  const [text, setText] = useState(initial);
  const dirty = text !== initial;
  return (
    <Section title="Objective">
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={4}
        className="w-full resize-y rounded-[10px] border px-3.5 py-3 text-sm leading-relaxed outline-none"
        style={{
          background: "var(--surface-2)",
          borderColor: "var(--border)",
          color: "var(--text)",
        }}
      />
      <div className="mt-3 flex items-center gap-3">
        <button
          onClick={() => onSave(text)}
          disabled={!dirty || saving}
          className="inline-flex h-[34px] items-center rounded-[8px] px-4 text-[13px] font-semibold disabled:opacity-40"
          style={{ background: "var(--text)", color: "var(--surface)" }}
        >
          {saving ? "Saving…" : "Save objective"}
        </button>
        {dirty && (
          <span className="text-xs" style={{ color: "var(--muted)" }}>
            Unsaved changes
          </span>
        )}
      </div>
    </Section>
  );
}

function RoleDiff({
  tailored,
  master,
}: {
  tailored: RoleBullets;
  master?: RoleBullets;
}) {
  const kept = new Set(tailored.bullets);
  const dropped = (master?.bullets ?? []).filter((b) => !kept.has(b));
  return (
    <div className="mb-5 last:mb-0">
      <div
        className="mb-2 text-[13px] font-semibold"
        style={{ color: "var(--text)" }}
      >
        {tailored.role} — {tailored.company}
      </div>
      <ul className="space-y-1.5">
        {tailored.bullets.map((b, i) => (
          <li
            key={`k-${i}`}
            className="flex gap-2 text-[13px] leading-relaxed"
            style={{ color: "var(--label)" }}
          >
            <span style={{ color: "var(--good)" }}>+</span>
            <span>{b}</span>
          </li>
        ))}
        {dropped.map((b, i) => (
          <li
            key={`d-${i}`}
            className="flex gap-2 text-[13px] leading-relaxed line-through"
            style={{ color: "var(--subtle)" }}
          >
            <span>−</span>
            <span>{b}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
