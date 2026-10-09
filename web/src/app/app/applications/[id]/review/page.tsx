// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { apiFetch, newRequestId } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { auth } from "@/lib/firebase";
import {
  failedMessage,
  isSubmitUnavailable,
  loadErrorMessage,
  markAppliedErrorMessage,
  objectiveErrorMessage,
  reviewActions,
  submitErrorMessage,
  type ReviewActions,
} from "@/lib/reviewActions";
import type { Application, RoleBullets } from "@/lib/types";
import { TopNav } from "@/components/TopNav";
import { MonoLabel } from "@/components/warm/Editable";
import { SERIF } from "@/components/warm/styles";
import { statusPill } from "../../status";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

/** Fetch the tailored resume (auth header) and trigger a browser download. */
async function downloadResume(appId: string, company: string): Promise<void> {
  const token = await auth.currentUser?.getIdToken();
  const requestId = newRequestId();
  const res = await fetch(`${API_BASE}/applications/${appId}/resume`, {
    headers: {
      "X-Request-Id": requestId,
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
  if (!res.ok) {
    console.error(`api ${res.status} resume download (request ${requestId})`);
    window.alert("Could not download the resume.");
    return;
  }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = `resume_${company.replace(/\s+/g, "_")}.docx`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export default function ReviewPage() {
  const { user, loading } = useAuth();
  const params = useParams<{ id: string }>();
  const id = params.id;
  const queryClient = useQueryClient();
  const queryKey = ["application", id] as const;

  const { data: app, isLoading, error } = useQuery({
    queryKey,
    queryFn: () => apiFetch<Application>(`/applications/${id}`),
    enabled: !!user,
    // Poll while work is queued or in flight, then stop. (The SSE stream below
    // pushes faster updates; this polling is the safety net.) "queued" has to
    // be here: an application waits there until the background task claims it,
    // and without it the page would stop polling forever on the exact state
    // this status was introduced to make visible.
    refetchInterval: (q) =>
      q.state.data?.status === "queued" ||
      q.state.data?.status === "tailoring" ||
      q.state.data?.status === "submitting"
        ? 3000
        : false,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey });
  // The tracking list shows status too, so a change here refreshes it as well.
  const invalidateAll = () =>
    Promise.all([
      invalidate(),
      queryClient.invalidateQueries({ queryKey: ["applications"] }),
    ]);

  const [objectiveError, setObjectiveError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitUnavailable, setSubmitUnavailable] = useState(false);
  const [markError, setMarkError] = useState<string | null>(null);

  // Live submission progress via SSE. EventSource can't set headers, so both
  // the Firebase token and the correlation id ride as query params (the token
  // is verified server-side; the id is adopted as X-Request-Id by the
  // middleware so the stream shares the page's request trail).
  useEffect(() => {
    if (app?.status !== "submitting") return;
    let es: EventSource | null = null;
    let cancelled = false;
    (async () => {
      const token = await auth.currentUser?.getIdToken();
      if (cancelled || !token) return;
      const requestId = newRequestId();
      es = new EventSource(
        `${API_BASE}/applications/${id}/events?token=${token}` +
          `&request_id=${requestId}`,
      );
      const refresh = () =>
        queryClient.invalidateQueries({ queryKey });
      es.addEventListener("progress", refresh);
      es.addEventListener("status", refresh);
      es.onerror = () => es?.close();
    })();
    return () => {
      cancelled = true;
      es?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app?.status, id]);

  const saveObjective = useMutation({
    mutationFn: (objective_text: string) =>
      apiFetch(`/applications/${id}/objective`, {
        method: "PUT",
        body: JSON.stringify({ objective_text }),
      }),
    onMutate: () => setObjectiveError(null),
    onSuccess: invalidateAll,
    onError: (err) => setObjectiveError(objectiveErrorMessage(err)),
  });

  const regenerate = useMutation({
    mutationFn: () =>
      apiFetch(`/applications/${id}/regenerate`, { method: "POST" }),
    onSuccess: invalidate,
  });

  const submit = useMutation({
    mutationFn: () =>
      apiFetch(`/applications/${id}/submit`, { method: "POST" }),
    onMutate: () => setSubmitError(null),
    onSuccess: invalidateAll,
    onError: (err) => {
      if (isSubmitUnavailable(err)) setSubmitUnavailable(true);
      setSubmitError(submitErrorMessage(err));
    },
  });

  const markApplied = useMutation({
    mutationFn: () =>
      apiFetch(`/applications/${id}/mark-applied`, { method: "POST" }),
    onMutate: () => setMarkError(null),
    onSuccess: invalidateAll,
    onError: (err) => setMarkError(markAppliedErrorMessage(err)),
  });

  if (loading || !user || isLoading) {
    return (
      <>
        <TopNav section="applications" />
        <main className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
          Loading…
        </main>
      </>
    );
  }

  if (error || !app) {
    return (
      <>
        <TopNav section="applications" />
        <main className="p-8 text-[13.5px]" style={{ color: "var(--brick)" }}>
          {loadErrorMessage(error)}
        </main>
      </>
    );
  }

  const pill = statusPill(app.status);
  const actions = reviewActions(app, { submitUnavailable });
  const company = app.job_company || "the employer";
  const site = app.job_company ? `${app.job_company}'s site` : "the employer's site";

  return (
    <>
      <TopNav section="applications" />
      <main className="mx-auto w-full max-w-[920px] flex-1 px-7 py-7">
        <div className="mb-5 flex items-start justify-between gap-5">
          <div>
            <h1
              className="text-[28px] font-normal leading-[1.15]"
              style={{ fontFamily: SERIF, color: "var(--ink)" }}
            >
              {app.job_title ?? app.job_id}
            </h1>
            <div
              className="mt-1.5 flex items-center gap-3 text-[13px]"
              style={{ color: "var(--ink-4)" }}
            >
              <span>{app.job_company ?? ""}</span>
              {app.job_url && (
                <a
                  href={app.job_url}
                  target="_blank"
                  rel="noreferrer"
                  className="wm-link font-semibold"
                >
                  View posting ↗
                </a>
              )}
            </div>
          </div>
          <span
            className="inline-flex flex-none items-center gap-[7px] rounded-full border px-3 py-[5px] text-[11.5px] font-bold"
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

        {(app.status === "queued" || app.status === "tailoring") && (
          <Banner>
            We&apos;re writing your objective and resume for this job. This page
            updates on its own.
          </Banner>
        )}
        {/* "queued" belongs on the banner side, not here: there is no objective
            or resume variant yet, so this branch would render a blank editor
            whose contents the tailoring result overwrites when it lands. */}
        {actions.showObjective && (
          <>
            {actions.objectiveEditable ? (
              <ObjectiveEditor
                key={app.objective_text ?? ""}
                initial={app.objective_text ?? ""}
                saving={saveObjective.isPending}
                error={objectiveError}
                onSave={(t) => saveObjective.mutate(t)}
              />
            ) : (
              <Section title={OBJECTIVE_TITLE}>
                <p
                  className="whitespace-pre-wrap text-[13.5px] leading-[1.65]"
                  style={{ color: "var(--ink-2)" }}
                >
                  {app.objective_text || "No objective for this one."}
                </p>
              </Section>
            )}

            <Section title="What we changed on your resume">
              <p
                className="mb-4 text-[13px] leading-[1.6]"
                style={{ color: "var(--ink-4)" }}
              >
                We moved the lines that matter most for this job to the top, and
                set the rest aside. Crossed-out lines won&apos;t be sent.
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
          </>
        )}

        <SubmissionPanel app={app} title={actions.panelTitle} />

        {actions.showManual && (
          <ManualApply
            app={app}
            actions={actions}
            marking={markApplied.isPending}
            error={markError}
            onMarkApplied={() => {
              if (
                window.confirm(
                  `Mark this as applied? Do this after you've submitted on ${site}.`,
                )
              )
                markApplied.mutate();
            }}
          />
        )}

        {submitError && (
          <p className="mt-4 text-[13px]" style={{ color: "var(--brick)" }}>
            {submitError}
          </p>
        )}

        <div className="mt-[22px] flex flex-wrap items-center gap-3">
          {actions.showSubmit && (
            <button
              onClick={() => {
                const verb =
                  app.status === "failed" ? "Retry submitting" : "Submit";
                if (
                  window.confirm(
                    `${verb} a real application to ${company} for "${app.job_title ?? app.job_id}"? This cannot be undone.`,
                  )
                )
                  submit.mutate();
              }}
              disabled={submit.isPending}
              className="wm-ghost inline-flex h-[42px] items-center gap-2 rounded-[13px] border px-[18px] text-[13.5px] font-semibold disabled:opacity-40"
              style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
            >
              ✓ {actions.submitLabel}
            </button>
          )}
          <button
            onClick={() => regenerate.mutate()}
            disabled={
              app.status === "tailoring" ||
              app.status === "submitting" ||
              regenerate.isPending
            }
            className="wm-ghost inline-flex h-[42px] items-center gap-1.5 rounded-[13px] border px-[18px] text-[13.5px] font-semibold disabled:opacity-40"
            style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
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
      className="mb-5 rounded-[14px] border px-4 py-3 text-[13px] leading-[1.6]"
      style={{
        background: danger ? "#fdf5f2" : "#fbf6ef",
        borderColor: danger ? "#f0c8bd" : "#e8dacb",
        color: danger ? "var(--brick)" : "var(--ink-3)",
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
      className="mb-3.5 rounded-[18px] border p-5"
      style={{ background: "var(--surface-warm)", borderColor: "var(--border-warm-hair)" }}
    >
      <h2
        className="mb-3 text-[15px] font-bold"
        style={{ color: "var(--ink)" }}
      >
        {title}
      </h2>
      {children}
    </section>
  );
}

const OBJECTIVE_TITLE = "Why you want this job — in your words";

function ObjectiveEditor({
  initial,
  saving,
  error,
  onSave,
}: {
  initial: string;
  saving: boolean;
  error: string | null;
  onSave: (text: string) => void;
}) {
  const [text, setText] = useState(initial);
  const dirty = text !== initial;
  return (
    <Section title={OBJECTIVE_TITLE}>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={4}
        className="wm-input w-full resize-y rounded-[14px] px-3.5 py-3.5 text-[13.5px] leading-[1.65] outline-none"
      />
      <div className="mt-3.5 flex items-center gap-3.5">
        <button
          onClick={() => onSave(text)}
          disabled={!dirty || saving}
          className="wm-cta inline-flex h-[38px] items-center rounded-[11px] px-[18px] text-[13px] font-semibold"
        >
          {saving ? "Saving…" : "Save this"}
        </button>
        {dirty && (
          <span className="text-[12.5px]" style={{ color: "#a3927f" }}>
            Not saved yet — press Save this.
          </span>
        )}
      </div>
      {error && (
        <p className="mt-2.5 text-[12.5px]" style={{ color: "var(--brick)" }}>
          {error}
        </p>
      )}
    </Section>
  );
}

function SubmissionPanel({
  app,
  title,
}: {
  app: Application;
  title: string | null;
}) {
  if (title === null) return null;
  const failed = app.status === "failed";

  // User-facing progress notes. Failure notes are raw errors, so they stay out.
  const notes = app.timeline.filter(
    (e) => e.note && ["submitting", "submitted"].includes(e.status),
  );

  return (
    <section
      className="mt-6 rounded-[18px] border p-[22px]"
      style={{
        background: failed ? "#fdf5f2" : "var(--surface-warm)",
        borderColor: failed ? "#f0c8bd" : "var(--border-warm-hair)",
      }}
    >
      <h2 className="mb-2 text-[16px] font-bold" style={{ color: "var(--ink)" }}>
        {title}
      </h2>

      {failed && (
        <p
          className="text-[13.5px] leading-[1.6]"
          style={{ color: "var(--ink-3)" }}
        >
          {failedMessage(app)}
        </p>
      )}

      {notes.length > 0 && (
        <div className="mt-[18px]">
          <MonoLabel>What happened</MonoLabel>
          <ol className="mt-3 space-y-2">
            {notes.map((e, i) => (
              <li key={i} className="flex gap-3 text-[12.5px] leading-[1.6]">
                <span className="min-w-[64px]" style={{ color: "#b0a08d" }}>
                  {new Date(e.at).toLocaleTimeString()}
                </span>
                <span style={{ color: e.status === "failed" ? "var(--brick)" : "var(--ink-3)" }}>{e.note}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {app.confirmation?.screenshot_uri && (
        <p
          className="mt-3 break-all text-[11px]"
          style={{ color: "#a3927f" }}
        >
          Confirmation screenshot: {app.confirmation.screenshot_uri}
        </p>
      )}
    </section>
  );
}

/** The trial's primary path: the user applies on the employer's site. */
function ManualApply({
  app,
  actions,
  marking,
  error,
  onMarkApplied,
}: {
  app: Application;
  actions: ReviewActions;
  marking: boolean;
  error: string | null;
  onMarkApplied: () => void;
}) {
  const [copied, setCopied] = useState<"ok" | "failed" | null>(null);
  const objective = app.objective_text ?? "";

  const copyObjective = async () => {
    try {
      await navigator.clipboard.writeText(objective);
      setCopied("ok");
    } catch {
      setCopied("failed");
    }
    window.setTimeout(() => setCopied(null), 2500);
  };

  const ghost =
    "wm-ghost inline-flex h-[42px] items-center gap-1.5 rounded-[12px] border px-[16px] text-[13.5px] font-semibold disabled:opacity-40";

  return (
    <section
      className="mt-6 rounded-[18px] border p-[22px]"
      style={{ background: "var(--surface-warm)", borderColor: "var(--border-warm-hair)" }}
    >
      <h2 className="mb-1.5 text-[16px] font-bold" style={{ color: "var(--ink)" }}>
        Send it yourself
      </h2>
      <p className="mb-[18px] text-[13px] leading-[1.6]" style={{ color: "var(--ink-4)" }}>
        Open the posting, attach the résumé we wrote, paste your objective if
        they ask for one, then come back and mark it as applied.
      </p>
      <div className="flex flex-wrap items-center gap-2.5">
        {app.job_url && (
          <a
            href={app.job_url}
            target="_blank"
            rel="noopener noreferrer"
            className="wm-cta inline-flex h-[42px] items-center gap-1.5 rounded-[12px] px-[18px] text-[13.5px] font-semibold"
          >
            Apply on {app.job_company || "the employer's site"} ↗
          </a>
        )}
        {actions.showDownload && (
          <button
            onClick={() => downloadResume(app.id, app.job_company ?? "company")}
            className={ghost}
            style={{ borderColor: "#e8dacb", color: "var(--ink)" }}
          >
            ↓ Download résumé
          </button>
        )}
        <button
          onClick={copyObjective}
          disabled={!objective}
          className={ghost}
          style={{ borderColor: "#e8dacb", color: "var(--ink)" }}
        >
          {copied === "ok" ? "Copied ✓" : "Copy objective"}
        </button>
        <button
          onClick={onMarkApplied}
          disabled={marking}
          className={ghost}
          style={{ borderColor: "#cfe0c8", color: "var(--sage)" }}
        >
          {marking ? "Saving…" : "I applied ✓"}
        </button>
      </div>
      {copied === "failed" && (
        <p className="mt-2.5 text-[12.5px]" style={{ color: "var(--brick)" }}>
          Could not copy. Select the objective above and copy it yourself.
        </p>
      )}
      {error && (
        <p className="mt-2.5 text-[12.5px]" style={{ color: "var(--brick)" }}>
          {error}
        </p>
      )}
    </section>
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
        className="mb-2.5 text-[13px] font-bold"
        style={{ color: "var(--ink)" }}
      >
        {tailored.role} — {tailored.company}
      </div>
      <ul className="space-y-2">
        {tailored.bullets.map((b, i) => (
          <li
            key={`k-${i}`}
            className="flex gap-2.5 text-[13px] leading-[1.6]"
            style={{ color: "var(--ink-2)" }}
          >
            <span className="font-bold" style={{ color: "var(--sage)" }}>+</span>
            <span>{b}</span>
          </li>
        ))}
        {dropped.map((b, i) => (
          <li
            key={`d-${i}`}
            className="flex gap-2.5 text-[13px] leading-[1.6] line-through"
            style={{ color: "#b0a08d" }}
          >
            <span className="no-underline">−</span>
            <span>{b}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
