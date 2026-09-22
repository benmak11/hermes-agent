// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { TopNav } from "@/components/TopNav";
import { InlineText, MonoLabel } from "@/components/warm/Editable";
import { Pill } from "@/components/warm/Pill";
import { CARD, SERIF } from "@/components/warm/styles";
import { useAuth } from "@/lib/auth";
import {
  LOOP_TEMPLATES,
  buildStagesFromTemplate,
  draftToStages,
  emptyStage,
  fromLocalInput,
  hasData,
  toLocalInput,
} from "@/lib/journeyEdit";
import { useJourneyById, useJourneyMutations } from "@/lib/journeys";
import type { Journey, JourneyStage } from "@/lib/types";

/**
 * Warm Flow screen 17 — the loop builder. Opened after the recruiter call,
 * when the user finally knows the shape of the process. Everything is local
 * state until "Looks right", which is one whole-document PUT.
 *
 * The rule the page exists to protect: a rebuild may never lose a stage that
 * carries history. Two guards — a data-carrying row has no × (it shows a
 * "✓ kept" pill instead), and `draftToStages` re-adds anything dropped.
 */

const PANEL = {
  background: "#fdf7ee",
  borderColor: "var(--border-warm-hair)",
} as const;

const ROW = {
  background: "var(--surface-warm)",
  borderColor: "var(--border-warm)",
} as const;

export default function LoopBuilderPage() {
  const { id } = useParams<{ id: string }>();
  const { user, loading } = useAuth();
  const router = useRouter();
  const lookup = useJourneyById(id, !!user);
  const { save } = useJourneyMutations();

  const [draft, setDraft] = useState<JourneyStage[]>([]);
  const [picked, setPicked] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");

  // Seeded once, from the *stored* stages: reopening the builder shows the
  // loop as saved, not a blank template.
  const journey = lookup.state === "ready" ? lookup.journey : null;
  const seeded = useRef(false);
  useEffect(() => {
    if (!journey || seeded.current) return;
    seeded.current = true;
    setDraft(journey.stages);
  }, [journey]);

  if (loading || !user || lookup.state === "loading") {
    return (
      <>
        <TopNav section="journeys" />
        <main className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
          Loading…
        </main>
      </>
    );
  }
  if (lookup.state !== "ready") {
    return (
      <Shell>
        <Notice bad={lookup.state === "error"}>
          {lookup.state === "error"
            ? "Couldn't load your journeys."
            : "We couldn't find that journey."}
        </Notice>
      </Shell>
    );
  }
  const j: Journey = lookup.journey;

  const patch = (i: number, next: Partial<JourneyStage>) =>
    setDraft((d) => d.map((s, n) => (n === i ? { ...s, ...next } : s)));
  const move = (i: number, by: number) =>
    setDraft((d) => {
      const next = [...d];
      const [s] = next.splice(i, 1);
      next.splice(i + by, 0, s);
      return next;
    });
  const confirm = () =>
    save.mutate(
      { ...j, stages: draftToStages(draft, j.stages) },
      { onSuccess: () => router.push("/app/journeys") },
    );
  const commitAdd = () => {
    const name = newName.trim();
    if (name) setDraft((d) => [...d, emptyStage(name)]);
    setNewName("");
    setAdding(false);
  };

  return (
    <Shell>
      <div
        className="rounded-[22px] border px-8 py-[30px]"
        style={{
          background: "var(--surface-warm)",
          borderColor: "var(--border-warm-hair)",
          boxShadow: CARD.boxShadow,
        }}
      >
        <h1
          className="text-[28px] font-normal"
          style={{ fontFamily: SERIF, color: "var(--ink)" }}
        >
          What does the rest of the process look like?
        </h1>
        <p className="mt-2 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
          {j.role} · {j.company} — add the stages you know about. Nothing here is locked in.
        </p>

        <div className="mt-[22px] grid gap-4 md:grid-cols-2">
          <section className="rounded-[18px] border p-5" style={PANEL}>
            <MonoLabel>Start from a template</MonoLabel>
            <div className="mt-[14px] flex flex-col gap-[9px]">
              {LOOP_TEMPLATES.filter((t) => t.stages.length > 0).map((t) => (
                <button
                  key={t.id}
                  className={`wm-tpl ${picked === t.id ? "wm-tpl-on" : ""} flex w-full items-center gap-[11px] rounded-[12px] px-[14px] py-[11px] text-[13px] font-semibold`}
                  style={{
                    background: "var(--surface-warm)",
                    borderStyle: "solid",
                    borderWidth: picked === t.id ? 2 : 1,
                    color: "var(--ink)",
                  }}
                  onClick={() => {
                    setPicked(t.id);
                    setDraft(buildStagesFromTemplate(t.stages, j.stages));
                  }}
                >
                  {t.label}
                  <span className="flex-1" />
                  <span className="font-normal" style={{ color: "var(--ink-4)" }}>
                    {t.stages.length} stages
                  </span>
                </button>
              ))}
              <button
                className="flex w-full items-center gap-[11px] rounded-[12px] px-[14px] py-[11px] text-[13px] font-semibold"
                style={{
                  background: "var(--surface-warm)",
                  border: "1px dashed #d9c4a8",
                  color: "var(--terracotta-d)",
                }}
                onClick={() => {
                  setPicked("scratch");
                  setDraft(buildStagesFromTemplate([], j.stages));
                }}
              >
                Build it from scratch
              </button>
            </div>
          </section>

          {/* Screen 17's paste path is a Gemini call (money). Deliberately
              inert in PR 10: no onClick, no handler, no network call at all.
              Do not wire this up without the user asking. */}
          <section className="flex flex-col rounded-[18px] border p-5" style={PANEL}>
            <MonoLabel>Or paste what they sent you</MonoLabel>
            <textarea
              disabled
              readOnly
              value=""
              placeholder="“Next would be a 60-min system design with one of our staff engineers, then a 45-min hiring manager conversation…”"
              className="wm-input mt-[14px] min-h-[148px] flex-1 rounded-[12px] p-[14px] text-[12.5px] leading-[1.7] outline-none"
              style={{ opacity: 0.75 }}
            />
            <button
              disabled
              className="wm-cta mt-3 h-10 w-full rounded-[12px] text-[13px] font-semibold disabled:opacity-40"
            >
              Read this and draft my stages
            </button>
            <p className="mt-2 text-[11.5px]" style={{ color: "#a3927f" }}>
              Coming soon — for now, start from a template.
            </p>
          </section>
        </div>

        <section
          className="mt-[18px] rounded-[18px] border px-[22px] py-5"
          style={{ background: "var(--terracotta-tint)", borderColor: "var(--border-warm)" }}
        >
          <div className="flex flex-wrap items-center gap-3">
            <MonoLabel color="var(--terracotta-d)">Draft — check this before we save it</MonoLabel>
            <span className="flex-1" />
            <span className="text-[12px]" style={{ color: "var(--ink-4)" }}>
              Reorder with ↑ ↓ · tap to edit
            </span>
          </div>

          <div className="mt-[14px] flex flex-col gap-[9px]">
            {draft.length === 0 && (
              <p className="text-[12.5px]" style={{ color: "#a3927f" }}>
                Nothing in the draft yet — pick a template or add a stage.
              </p>
            )}
            {draft.map((s, i) => (
              <div
                key={s.id}
                className="flex flex-wrap items-center gap-3 rounded-[12px] border px-[15px] py-3"
                style={ROW}
              >
                <span className="flex flex-none flex-col" style={{ color: "#b0a08d" }}>
                  <button
                    aria-label={`Move ${s.name} up`}
                    disabled={i === 0}
                    className="text-[10px] leading-[8px] disabled:opacity-30"
                    onClick={() => move(i, -1)}
                  >
                    ↑
                  </button>
                  <button
                    aria-label={`Move ${s.name} down`}
                    disabled={i === draft.length - 1}
                    className="text-[10px] leading-[8px] disabled:opacity-30"
                    onClick={() => move(i, 1)}
                  >
                    ↓
                  </button>
                </span>
                <span className="min-w-[170px]">
                  <InlineText
                    value={s.name}
                    onSave={(n) => patch(i, { name: n })}
                    textClass="text-[13.5px] font-bold"
                    placeholder="stage name"
                  />
                </span>
                <input
                  className="wm-input h-[26px] w-[150px] rounded-[9px] px-2 text-[11.5px] outline-none"
                  placeholder="60 min · video"
                  aria-label={`Format for ${s.name}`}
                  value={s.format ?? ""}
                  onChange={(e) => patch(i, { format: e.target.value || null })}
                />
                <input
                  className="wm-input h-[26px] min-w-0 flex-1 rounded-[9px] px-2 text-[12.5px] outline-none"
                  placeholder="who with — e.g. Priya"
                  aria-label={`Who ${s.name} is with`}
                  value={s.who ?? ""}
                  onChange={(e) => patch(i, { who: e.target.value || null })}
                />
                <input
                  type="datetime-local"
                  className="wm-input h-[26px] rounded-[9px] px-2 text-[12px] outline-none"
                  aria-label={`Date for ${s.name}`}
                  value={toLocalInput(s.scheduled_at)}
                  onChange={(e) => patch(i, { scheduled_at: fromLocalInput(e.target.value) })}
                />
                {hasData(s) ? (
                  <Pill tone="good">✓ kept</Pill>
                ) : (
                  <button
                    aria-label={`Remove ${s.name}`}
                    className="wm-nav-quiet text-[13px]"
                    onClick={() => setDraft((d) => d.filter((_, n) => n !== i))}
                  >
                    ×
                  </button>
                )}
              </div>
            ))}

            {adding ? (
              <input
                autoFocus
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitAdd();
                  if (e.key === "Escape") {
                    // Clear before closing: onBlur commits, and an unmount can fire
                    // focusout first, which would add the row the user just cancelled.
                    setNewName("");
                    setAdding(false);
                  }
                }}
                onBlur={commitAdd}
                placeholder="stage — e.g. Values conversation"
                className="wm-input h-[38px] self-start rounded-[12px] px-3 text-[12.5px] outline-none"
              />
            ) : (
              <button
                className="self-start rounded-[12px] px-[15px] py-2.5 text-[12.5px] font-semibold"
                style={{
                  background: "var(--surface-warm)",
                  border: "1px dashed #d9c4a8",
                  color: "var(--terracotta-d)",
                }}
                onClick={() => setAdding(true)}
              >
                + Add a stage they didn&apos;t mention
              </button>
            )}
          </div>

          <div className="mt-[18px] flex flex-wrap items-center gap-3.5">
            <button
              className="wm-cta h-[42px] rounded-[12px] px-5 text-[13.5px] font-semibold disabled:opacity-40"
              disabled={save.isPending}
              onClick={confirm}
            >
              Looks right — save my map
            </button>
            <span className="text-[12.5px]" style={{ color: "var(--ink-4)" }}>
              You can change any of this later. Nothing here is locked in.
            </span>
          </div>
          {save.isError && (
            <p className="mt-2 text-[11.5px]" style={{ color: "var(--brick)" }}>
              {`Couldn't save that — ${String(save.error)}`}
            </p>
          )}
        </section>
      </div>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <>
      <TopNav section="journeys" />
      <main className="mx-auto w-full max-w-[1100px] flex-1 px-[30px] pt-[26px] pb-7">
        <Link href="/app/journeys" className="wm-nav-quiet mb-3 inline-block text-[12px]">
          ← Your journeys
        </Link>
        {children}
      </main>
    </>
  );
}

function Notice({ children, bad }: { children: React.ReactNode; bad: boolean }) {
  return (
    <div
      className="rounded-[18px] border px-6 py-8 text-center text-[13.5px]"
      style={{
        background: "var(--surface-warm)",
        borderColor: "var(--border-warm-hair)",
        color: bad ? "var(--brick)" : "var(--ink-2)",
      }}
    >
      {children}
    </div>
  );
}
