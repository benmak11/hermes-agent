// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { TopNav } from "@/components/TopNav";
import { CompanyTile, tileHue } from "@/components/warm/CompanyTile";
import { InlineText, MonoLabel, PencilBtn } from "@/components/warm/Editable";
import { Pill } from "@/components/warm/Pill";
import { CARD, SANS, SERIF } from "@/components/warm/styles";
import { useAuth } from "@/lib/auth";
import { fromLocalInput, relativeWhen, seedPrep, toLocalInput } from "@/lib/journeyEdit";
import { useJourneyById, useJourneyMutations, useJourneys } from "@/lib/journeys";
import type { Journey, JourneyStage, PrepItem, StageQuestion } from "@/lib/types";
import { initial } from "@/lib/ui";

/**
 * Warm Flow screen 18 — one stage. The facts (when, format, who), the prep
 * checklist (seeded as *suggestions* from the user's past journeys — opening
 * a stage never writes), and the questions they asked, which PR 12's insights
 * screen aggregates. Every edit is its own whole-document PUT.
 *
 * Two things the design shows that `JourneyStage` cannot store and that this
 * page therefore omits: a "Where / Meet link" row (no location field) and a
 * duration (`scheduled_at` is one instant, so only the start renders).
 */

/** The violet accent for a question's topic chip. `CompanyTile`'s `violet`
 *  hue is the same pair but isn't exported or tokenised; two chips don't
 *  justify a global `--violet*`. */
const TOPIC = { bg: "#f3edf7", fg: "#7a5a94" } as const;

const PANEL = {
  background: "#fdf7ee",
  borderColor: "var(--border-warm-hair)",
} as const;

export default function StageDetailPage() {
  const { id, stageId } = useParams<{ id: string; stageId: string }>();
  const { user, loading } = useAuth();
  const lookup = useJourneyById(id, !!user);
  const all = useJourneys(!!user);
  const { save } = useJourneyMutations();

  const [addingPrep, setAddingPrep] = useState(false);
  const [prepDraft, setPrepDraft] = useState("");
  const [addingQ, setAddingQ] = useState(false);
  const [qText, setQText] = useState("");
  const [qTopic, setQTopic] = useState("");
  const [qFelt, setQFelt] = useState<StageQuestion["felt"]>(null);
  const [editTopic, setEditTopic] = useState<number | null>(null);
  const [topicDraft, setTopicDraft] = useState("");

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
  const idx = j.stages.findIndex((s) => s.id === stageId);
  if (idx < 0) {
    return (
      <Shell>
        <Notice bad={false}>That stage isn&apos;t on this journey any more.</Notice>
      </Shell>
    );
  }
  const stage = j.stages[idx];
  const now = new Date();
  const journeys = all.data?.journeys ?? [];
  const rel =
    stage.status !== "done" && stage.scheduled_at
      ? relativeWhen(stage.scheduled_at, now)
      : null;
  const suggestions = seedPrep(journeys, j.id, stage);
  const carried = stage.prep.find((p) => p.from_journey_id);
  const carriedFrom = journeys.find((x) => x.id === carried?.from_journey_id);
  const carriedWhere = carriedFrom ? `your ${carriedFrom.company}` : "a past";
  const prepDone = stage.prep.filter((p) => p.done).length;

  const patchStage = (next: Partial<JourneyStage>) =>
    save.mutate({
      ...j,
      stages: j.stages.map((s) => (s.id === stageId ? { ...s, ...next } : s)),
    });
  const setPrep = (prep: PrepItem[]) => patchStage({ prep });
  const setQuestions = (questions: StageQuestion[]) => patchStage({ questions });
  const patchQuestion = (n: number, next: Partial<StageQuestion>) =>
    setQuestions(stage.questions.map((q, i) => (i === n ? { ...q, ...next } : q)));

  const commitPrep = () => {
    const text = prepDraft.trim();
    if (text) setPrep([...stage.prep, { text, done: false, from_journey_id: null }]);
    setPrepDraft("");
    setAddingPrep(false);
  };
  const commitQuestion = () => {
    const text = qText.trim();
    if (!text) return;
    setQuestions([...stage.questions, { text, topic: qTopic.trim() || null, felt: qFelt }]);
    setQText("");
    setQTopic("");
    setQFelt(null);
    setAddingQ(false);
  };

  return (
    <Shell>
      <div
        className="rounded-[22px] border px-8 py-7"
        style={{
          background: "var(--surface-warm)",
          borderColor: "var(--border-warm-hair)",
          boxShadow: CARD.boxShadow,
        }}
      >
        <div className="flex flex-wrap items-center gap-[13px]">
          <CompanyTile initial={initial(stage.name)} hue={tileHue(j.company)} />
          <div className="min-w-0 flex-1" style={{ fontFamily: SERIF }}>
            <InlineText
              value={stage.name}
              onSave={(n) => n && patchStage({ name: n })}
              textClass="text-[26px] font-normal"
            />
            <div
              className="mt-[5px] text-[12.5px]"
              style={{ color: "var(--ink-4)", fontFamily: SANS }}
            >
              {j.role} · {j.company} — stage {idx + 1} of {j.stages.length}
            </div>
          </div>
          {rel && <Pill tone="accent">{rel}</Pill>}
        </div>

        <div className="mt-[22px] grid items-start gap-4 md:grid-cols-2">
          <div className="flex flex-col gap-[14px]">
            <section className="rounded-[18px] border p-5" style={PANEL}>
              <div className="grid grid-cols-[96px_1fr] items-center gap-x-[14px] gap-y-3 text-[13px]">
                <span style={{ color: "#a3927f" }}>When</span>
                <input
                  type="datetime-local"
                  className="wm-input h-[30px] rounded-[9px] px-2 text-[13px] outline-none"
                  aria-label="When this stage is"
                  value={toLocalInput(stage.scheduled_at)}
                  onChange={(e) => patchStage({ scheduled_at: fromLocalInput(e.target.value) })}
                />
                <span style={{ color: "#a3927f" }}>Format</span>
                <InlineText
                  value={stage.format ?? ""}
                  placeholder="not set"
                  onSave={(v) => patchStage({ format: v || null })}
                  textClass="text-[13px] font-semibold"
                />
                <span style={{ color: "#a3927f" }}>With</span>
                <InlineText
                  value={stage.who ?? ""}
                  placeholder="not set"
                  onSave={(v) => patchStage({ who: v || null })}
                  textClass="text-[13px] font-semibold"
                />
              </div>
            </section>

            <section className="rounded-[18px] border p-5" style={PANEL}>
              <div className="flex items-center gap-2.5">
                <MonoLabel>Getting ready</MonoLabel>
                <span className="flex-1" />
                {stage.prep.length > 0 && (
                  <span className="text-[11.5px]" style={{ color: "var(--ink-4)" }}>
                    {prepDone} of {stage.prep.length} done
                  </span>
                )}
              </div>
              <div className="mt-[14px] flex flex-col gap-2.5">
                {stage.prep.map((p, n) => (
                  <div key={`${p.text}-${n}`} className="flex items-center gap-[11px] text-[13px]">
                    <button
                      role="checkbox"
                      aria-checked={p.done}
                      aria-label={p.text}
                      className="flex h-5 w-5 flex-none items-center justify-center rounded-[6px] text-[12px] font-bold"
                      style={
                        p.done
                          ? { background: "var(--sage)", color: "#f6faf3" }
                          : { border: "1px solid #d9c4a8", background: "var(--surface-warm)" }
                      }
                      onClick={() =>
                        setPrep(stage.prep.map((x, i) => (i === n ? { ...x, done: !x.done } : x)))
                      }
                    >
                      {p.done ? "✓" : ""}
                    </button>
                    <span
                      className="flex-1"
                      style={
                        p.done
                          ? { color: "#a3927f", textDecoration: "line-through" }
                          : { color: "var(--ink-2)" }
                      }
                    >
                      {p.text}
                    </span>
                    <button
                      aria-label={`Remove ${p.text}`}
                      className="wm-nav-quiet text-[13px]"
                      onClick={() => setPrep(stage.prep.filter((_, i) => i !== n))}
                    >
                      ×
                    </button>
                  </div>
                ))}
                {addingPrep ? (
                  <input
                    autoFocus
                    value={prepDraft}
                    onChange={(e) => setPrepDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitPrep();
                      if (e.key === "Escape") {
                        // Clear first: onBlur commits, and an unmount can fire
                        // focusout before the state update lands.
                        setPrepDraft("");
                        setAddingPrep(false);
                      }
                    }}
                    onBlur={commitPrep}
                    placeholder="something to prepare…"
                    className="wm-input h-[32px] self-start rounded-[10px] px-2.5 text-[12.5px] outline-none"
                  />
                ) : (
                  <button
                    className="self-start rounded-[10px] px-3 py-2 text-[12.5px] font-semibold"
                    style={{
                      background: "var(--surface-warm)",
                      border: "1px dashed #d9c4a8",
                      color: "var(--terracotta-d)",
                    }}
                    onClick={() => setAddingPrep(true)}
                  >
                    + Add something to prepare
                  </button>
                )}
              </div>

              {carried && (
                <div
                  className="mt-[14px] rounded-[12px] border px-[14px] py-3 text-[12.5px] leading-[1.6]"
                  style={{
                    background: "var(--terracotta-tint)",
                    borderColor: "var(--border-warm)",
                    color: "var(--ink-2)",
                  }}
                >
                  Carried over from {carriedWhere} journey.
                </div>
              )}

              {suggestions.length > 0 && (
                <div className="mt-[18px]">
                  <MonoLabel>From your past journeys</MonoLabel>
                  <div className="mt-2.5 flex flex-col gap-2">
                    {suggestions.map((s) => (
                      <div
                        key={s.text}
                        className="flex items-center gap-2.5 text-[12.5px]"
                        style={{ color: "var(--ink-2)" }}
                      >
                        <span className="flex-1">{s.text}</span>
                        <button
                          className="font-semibold"
                          style={{ color: "var(--terracotta-d)" }}
                          onClick={() => setPrep([...stage.prep, s])}
                        >
                          + add
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </section>
          </div>

          <section className="rounded-[18px] border p-5" style={PANEL}>
            <MonoLabel>Questions they asked</MonoLabel>
            <p className="mt-2 text-[12.5px]" style={{ color: "var(--ink-4)" }}>
              Jot these down while it&apos;s fresh. They feed your insights later.
            </p>
            <div className="mt-[14px] flex flex-col gap-[9px]">
              {stage.questions.map((q, n) => (
                <div
                  key={`${q.text}-${n}`}
                  className="rounded-[12px] border px-[14px] py-3"
                  style={{ background: "var(--surface-warm)", borderColor: "#e8dacb" }}
                >
                  <div className="flex items-start gap-2">
                    <span className="min-w-0 flex-1">
                      <InlineText
                        value={q.text}
                        onSave={(v) => v && patchQuestion(n, { text: v })}
                        textClass="text-[13px] leading-[1.6]"
                      />
                    </span>
                    <button
                      aria-label={`Remove question ${n + 1}`}
                      className="wm-nav-quiet flex-none text-[13px]"
                      onClick={() => setQuestions(stage.questions.filter((_, i) => i !== n))}
                    >
                      ×
                    </button>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-[7px]">
                    {editTopic === n ? (
                      <input
                        autoFocus
                        value={topicDraft}
                        onChange={(e) => setTopicDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            patchQuestion(n, { topic: topicDraft.trim() || null });
                            setEditTopic(null);
                          }
                          if (e.key === "Escape") setEditTopic(null);
                        }}
                        placeholder="topic"
                        className="wm-input h-[24px] w-28 rounded-[8px] px-2 text-[11px] outline-none"
                      />
                    ) : (
                      <>
                        {q.topic && (
                          <span
                            className="rounded-[8px] px-[9px] py-[3px] text-[11px] font-semibold"
                            style={{ background: TOPIC.bg, color: TOPIC.fg }}
                          >
                            {q.topic}
                          </span>
                        )}
                        <PencilBtn
                          label={`Edit topic for question ${n + 1}`}
                          onClick={() => {
                            setTopicDraft(q.topic ?? "");
                            setEditTopic(n);
                          }}
                        />
                      </>
                    )}
                    <FeltToggle
                      felt="good"
                      on={q.felt === "good"}
                      onClick={() => patchQuestion(n, { felt: q.felt === "good" ? null : "good" })}
                    />
                    <FeltToggle
                      felt="struggled"
                      on={q.felt === "struggled"}
                      onClick={() =>
                        patchQuestion(n, { felt: q.felt === "struggled" ? null : "struggled" })
                      }
                    />
                  </div>
                </div>
              ))}

              {addingQ ? (
                <div
                  className="flex flex-col gap-2 rounded-[12px] border px-[14px] py-3"
                  style={{ background: "var(--surface-warm)", borderColor: "#e8dacb" }}
                >
                  <input
                    autoFocus
                    value={qText}
                    onChange={(e) => setQText(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitQuestion();
                      if (e.key === "Escape") {
                        setQText("");
                        setQTopic("");
                        setAddingQ(false);
                      }
                    }}
                    placeholder="what did they ask?"
                    className="wm-input h-[32px] rounded-[10px] px-2.5 text-[12.5px] outline-none"
                  />
                  <div className="flex flex-wrap items-center gap-[7px]">
                    <input
                      value={qTopic}
                      onChange={(e) => setQTopic(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") commitQuestion();
                        if (e.key === "Escape") {
                          setQText("");
                          setQTopic("");
                          setAddingQ(false);
                        }
                      }}
                      placeholder="topic (optional)"
                      className="wm-input h-[26px] w-32 rounded-[8px] px-2 text-[11px] outline-none"
                    />
                    <FeltToggle
                      felt="good"
                      on={qFelt === "good"}
                      onClick={() => setQFelt(qFelt === "good" ? null : "good")}
                    />
                    <FeltToggle
                      felt="struggled"
                      on={qFelt === "struggled"}
                      onClick={() => setQFelt(qFelt === "struggled" ? null : "struggled")}
                    />
                    <span className="flex-1" />
                    <button
                      className="wm-cta h-[34px] rounded-[10px] px-3.5 text-[12.5px] font-semibold disabled:opacity-40"
                      disabled={!qText.trim()}
                      onClick={commitQuestion}
                    >
                      + Add
                    </button>
                    <button className="wm-nav-quiet text-[12px]" onClick={() => setAddingQ(false)}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  className="self-start rounded-[12px] px-[14px] py-[11px] text-[12.5px] font-semibold"
                  style={{
                    background: "var(--surface-warm)",
                    border: "1px dashed #d9c4a8",
                    color: "var(--terracotta-d)",
                  }}
                  onClick={() => setAddingQ(true)}
                >
                  + Add a question they asked
                </button>
              )}
            </div>

            <div className="mt-[18px] border-t pt-4" style={{ borderColor: "#f4ebdf" }}>
              <MonoLabel>When it&apos;s over</MonoLabel>
              {/* Screen 19. Saving from there completes the stage too when it
                  is still current, so this is one PUT, not two. An `upcoming`
                  stage has nothing to check in on yet, and a check-in written
                  there would be invisible on the board (noteFor only reads it
                  on a done stage). */}
              {stage.status === "upcoming" ? (
                <div
                  className="mt-3 flex h-[42px] w-full items-center justify-center rounded-[12px] border text-[13.5px] font-semibold opacity-50"
                  style={{
                    background: "var(--terracotta-tint)",
                    borderColor: "var(--border-warm)",
                    color: "var(--terracotta-d)",
                  }}
                >
                  Do the two-minute check-in
                </div>
              ) : (
                <Link
                  href={`/app/journeys/${j.id}/stages/${stage.id}/checkin`}
                  className="mt-3 flex h-[42px] w-full items-center justify-center rounded-[12px] border text-[13.5px] font-semibold"
                  style={{
                    background: "var(--terracotta-tint)",
                    borderColor: "var(--border-warm)",
                    color: "var(--terracotta-d)",
                  }}
                >
                  Do the two-minute check-in
                </Link>
              )}
              {stage.status === "upcoming" && (
                <p className="mt-2 text-[11.5px]" style={{ color: "#a3927f" }}>
                  After it happens — mark the stage done and we&apos;ll ask.
                </p>
              )}
            </div>
          </section>
        </div>

        {save.isError && (
          <p className="mt-3 text-[11.5px]" style={{ color: "var(--brick)" }}>
            {`Couldn't save that — ${String(save.error)}`}
          </p>
        )}
      </div>
    </Shell>
  );
}

function FeltToggle({
  felt,
  on,
  onClick,
}: {
  felt: "good" | "struggled";
  on: boolean;
  onClick: () => void;
}) {
  const label = felt === "good" ? "felt good" : "struggled";
  const style = on
    ? felt === "good"
      ? { background: "var(--sage-tint)", color: "var(--sage)" }
      : { background: "#fdf5f2", color: "var(--brick)" }
    : { background: "var(--surface-warm)", color: "#a3927f", border: "1px dashed #d9c4a8" };
  return (
    <button
      aria-pressed={on}
      onClick={onClick}
      className="rounded-[8px] px-[9px] py-[3px] text-[11px] font-semibold"
      style={style}
    >
      {label}
    </button>
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
