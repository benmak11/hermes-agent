// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

import { TopNav } from "@/components/TopNav";
import { MonoLabel } from "@/components/warm/Editable";
import { CARD, SERIF } from "@/components/warm/styles";
import { useAuth } from "@/lib/auth";
import { CHECKIN_TAGS, RATING_LABELS, applyCheckIn, buildCheckIn } from "@/lib/journeyEdit";
import { useJourneyById, useJourneyMutations } from "@/lib/journeys";
import { shortDate } from "@/lib/journeysDerive";
import type { CSSProperties } from "react";

/**
 * Warm Flow screen 19 — the after-a-stage check-in. Deliberately tiny: how it
 * felt, what came up, one optional sentence. A route rather than a dialog
 * because three separate triggers (the board's "✓ Done with X", the stage
 * page, the week strip's waiting chip) all need one destination, and the repo
 * has no dialog primitive to trap focus in.
 *
 * "Skip for now" writes NOTHING. A `checkin` of `{}` is truthy, so it would
 * drop the stage out of `thisWeek`'s waiting count and make the board print
 * "checked in" on a stage nobody checked in on. `buildCheckIn` is the only
 * constructor and returns null for an empty form.
 */

type Rating = 1 | 2 | 3 | 4 | 5;
const RATINGS: Rating[] = [1, 2, 3, 4, 5];

/** The design's per-rating palette (lines 40-44). Border colour is owned by
 *  `.wm-chip`/`.wm-chip-on`; selection only thickens it. */
const RATING_STYLE: Record<Rating, CSSProperties> = {
  1: { background: "#fdf5f2", color: "var(--brick)" },
  2: { background: "#fdf7ee", color: "var(--ink-4)" },
  3: { background: "var(--terracotta-tint)", color: "var(--terracotta-d)" },
  4: { background: "#fdf7ee", color: "var(--ink-4)" },
  5: { background: "#f6faf3", color: "var(--sage)" },
};

export default function CheckInPage() {
  const { id, stageId } = useParams<{ id: string; stageId: string }>();
  const router = useRouter();
  const { user, loading } = useAuth();
  const lookup = useJourneyById(id, !!user);
  const { save } = useJourneyMutations();

  // Seeded once from the stored check-in: re-editing one is the same screen.
  const stored = lookup.state === "ready" ? lookup.journey.stages.find((s) => s.id === stageId)?.checkin : null;
  const [seeded, setSeeded] = useState(false);
  const [rating, setRating] = useState<number | null>(null);
  const [tags, setTags] = useState<string[]>([]);
  const [sentence, setSentence] = useState("");
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");

  if (stored && !seeded) {
    setSeeded(true);
    setRating(stored.rating);
    setTags(stored.tags);
    setSentence(stored.sentence ?? "");
  }

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
  const j = lookup.journey;
  const stage = j.stages.find((s) => s.id === stageId);
  if (!stage) {
    return (
      <Shell>
        <Notice bad={false}>That stage isn&apos;t on this journey any more.</Notice>
      </Shell>
    );
  }

  const when = stage.scheduled_at ? shortDate(stage.scheduled_at) : "just now";
  // Custom tags the user typed sit in the same row, always selected.
  const extra = tags.filter((t) => !CHECKIN_TAGS.includes(t));
  const built = buildCheckIn(rating, tags, sentence, new Date().toISOString());

  const toggleTag = (tag: string) =>
    setTags((prev) => (prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]));

  function commitCustom() {
    const text = draft.trim();
    setDraft("");
    setAdding(false);
    if (!text) return;
    setTags((prev) =>
      prev.some((t) => t.toLowerCase() === text.toLowerCase()) ? prev : [...prev, text],
    );
  }

  const done = () => router.push("/app/journeys");

  return (
    <Shell>
      <div
        className="mx-auto w-full max-w-[760px] rounded-[22px] border px-8 py-[30px]"
        style={{
          background: "var(--surface-warm)",
          borderColor: "var(--border-warm-hair)",
          boxShadow: CARD.boxShadow,
        }}
      >
        <h1
          className="text-[27px] font-normal leading-[1.15]"
          style={{ fontFamily: SERIF, color: "var(--ink)" }}
        >
          {`How did the ${stage.name.toLowerCase()} go?`}
        </h1>
        <p className="mt-2 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
          {`${j.company} · ${when} — two minutes now saves you an hour of guessing later.`}
        </p>

        <div className="mt-6">
          <MonoLabel>Honestly, how did it feel?</MonoLabel>
          <div className="mt-3 flex flex-wrap gap-[9px]">
            {RATINGS.map((n) => {
              const on = rating === n;
              return (
                <button
                  key={n}
                  aria-pressed={on}
                  onClick={() => setRating(on ? null : n)}
                  className={`wm-chip min-w-[92px] flex-1 rounded-[12px] py-[13px] text-center text-[12.5px] font-semibold ${on ? "wm-chip-on" : ""}`}
                  style={{ ...RATING_STYLE[n], ...(on ? { borderWidth: 2 } : null) }}
                >
                  {RATING_LABELS[n]}
                </button>
              );
            })}
          </div>
        </div>

        <div className="mt-[22px]">
          <MonoLabel>What came up? Tap any that fit</MonoLabel>
          <div className="mt-3 flex flex-wrap gap-2">
            {[...CHECKIN_TAGS, ...extra].map((tag) => {
              const on = tags.includes(tag);
              return (
                <button
                  key={tag}
                  aria-pressed={on}
                  onClick={() => toggleTag(tag)}
                  className={`wm-chip rounded-[11px] px-[14px] py-2 text-[12.5px] font-semibold ${on ? "wm-chip-on" : ""}`}
                  style={{
                    background: on ? "var(--terracotta-tint)" : "#fdf7ee",
                    color: on ? "var(--terracotta-d)" : "var(--ink-4)",
                    ...(on ? { borderWidth: 2 } : null),
                  }}
                >
                  {tag}
                </button>
              );
            })}
            {adding ? (
              <input
                autoFocus
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitCustom();
                  if (e.key === "Escape") {
                    setDraft("");
                    setAdding(false);
                  }
                }}
                onBlur={commitCustom}
                placeholder="your own words…"
                aria-label="Add your own tag"
                className="wm-input h-[30px] w-44 rounded-[11px] px-2.5 text-[12.5px] outline-none"
              />
            ) : (
              <button
                onClick={() => setAdding(true)}
                className="rounded-[11px] px-[14px] py-2 text-[12.5px] font-semibold"
                style={{
                  background: "var(--surface-warm)",
                  border: "1px dashed #d9c4a8",
                  color: "var(--terracotta-d)",
                }}
              >
                + your own words
              </button>
            )}
          </div>
        </div>

        <div className="mt-[22px]">
          <div className="flex items-baseline">
            <MonoLabel>One thing you&apos;d do differently</MonoLabel>
            <span className="ml-1.5 text-[10.5px] font-semibold" style={{ color: "#b0a08d" }}>
              — optional
            </span>
          </div>
          <textarea
            rows={3}
            value={sentence}
            onChange={(e) => setSentence(e.target.value)}
            aria-label="One thing you'd do differently"
            placeholder="Spend the first five minutes on requirements instead of jumping into the data model."
            className="wm-input mt-3 min-h-[78px] w-full rounded-[12px] p-[14px] text-[13px] leading-[1.65] outline-none"
          />
        </div>

        <div className="mt-6 flex flex-wrap items-center gap-3.5">
          <button
            className="wm-cta h-11 rounded-[12px] px-[22px] text-[13.5px] font-semibold disabled:opacity-40"
            disabled={built === null || save.isPending}
            onClick={() =>
              save.mutate(applyCheckIn(j, stage.id, built), { onSuccess: done })
            }
          >
            Save the check-in
          </button>
          {/* No write at all: the stage stays waiting rather than storing an
              empty check-in object. */}
          <button className="wm-nav-quiet text-[13px]" onClick={done}>
            Skip for now
          </button>
          <span className="ml-auto text-[12.5px]" style={{ color: "#a3927f" }}>
            Only you ever see this.
          </span>
        </div>
        {built === null && (
          <p className="mt-2 text-[11.5px]" style={{ color: "#a3927f" }}>
            Pick how it felt, or tap a tag.
          </p>
        )}
        {save.isError && (
          <p className="mt-2 text-[11.5px]" style={{ color: "var(--brick)" }}>
            {`Couldn't save that — ${String(save.error)}`}
          </p>
        )}
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
