// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import type { CSSProperties } from "react";

import { TopNav } from "@/components/TopNav";
import { CompanyTile, tileHue } from "@/components/warm/CompanyTile";
import { MonoLabel } from "@/components/warm/Editable";
import { JourneyTrack } from "@/components/warm/JourneyTrack";
import { Pill } from "@/components/warm/Pill";
import { CARD, SERIF } from "@/components/warm/styles";
import { useAuth } from "@/lib/auth";
import {
  RETRO_COPY,
  checkInEvidence,
  closeJourney,
  endedAtStageId,
  retroSummary,
  retroTrack,
} from "@/lib/journeyEdit";
import { useJourneyById, useJourneyMutations } from "@/lib/journeys";
import { shortDate } from "@/lib/journeysDerive";
import type { Journey, JourneyOutcome } from "@/lib/types";
import { initial } from "@/lib/ui";

/**
 * Warm Flow screen 20 — the outcome retro, and the only place a journey is
 * actually closed. The board's three outcome buttons now *propose* an outcome
 * in `?outcome=`; this page commits it, so abandoning the retro leaves the
 * journey exactly as it was and a misclick is no longer destructive.
 *
 * On an already-closed journey the stored outcome always wins over the query
 * param — otherwise a bookmarked `?outcome=rejected` would silently
 * reclassify an offer on the next save.
 */

const OUTCOMES: JourneyOutcome[] = ["offer", "rejected", "withdrawn"];

type PromptKey = "keep" | "change" | "next";
const PROMPT_KEYS: PromptKey[] = ["keep", "change", "next"];

/** Palettes follow the design regardless of outcome (keep = sage, change =
 *  brick, next = terracotta). `--terracotta-d` on a 10.5px label, per the
 *  seam doc's contrast rule. */
const PROMPT: Record<PromptKey, { panel: CSSProperties; label: string }> = {
  keep: { panel: { background: "#f6faf3", borderColor: "#cfe0c8" }, label: "var(--sage)" },
  change: { panel: { background: "#fdf5f2", borderColor: "#f0c8bd" }, label: "var(--brick)" },
  next: {
    panel: { background: "var(--terracotta-tint)", borderColor: "var(--border-warm)" },
    label: "var(--terracotta-d)",
  },
};

/** The design's five celebration specks (lines 84-88), lefts as percentages of
 *  the card so they survive a narrow viewport. Static — no animation. */
const SPECKS: CSSProperties[] = [
  { top: 12, left: "46.4%", width: 7, height: 12, borderRadius: 2, transform: "rotate(24deg)", background: "var(--honey)" },
  { top: 32, left: "50.4%", width: 7, height: 7, borderRadius: "50%", background: "var(--terracotta)" },
  { top: 9, left: "54.3%", width: 7, height: 12, borderRadius: 2, transform: "rotate(-18deg)", background: "var(--sage)" },
  { top: 34, left: "58.7%", width: 7, height: 12, borderRadius: 2, transform: "rotate(40deg)", background: "var(--terracotta)" },
  { top: 13, left: "62.6%", width: 7, height: 7, borderRadius: "50%", background: "var(--honey)" },
];

export default function RetroPage() {
  // useSearchParams needs a Suspense boundary for prerendering.
  return (
    <Suspense
      fallback={
        <div className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
          Loading…
        </div>
      }
    >
      <RetroInner />
    </Suspense>
  );
}

function RetroInner() {
  const { id } = useParams<{ id: string }>();
  const params = useSearchParams();
  const router = useRouter();
  const { user, loading } = useAuth();
  const lookup = useJourneyById(id, !!user);
  const { save } = useJourneyMutations();

  const [seeded, setSeeded] = useState(false);
  const [keep, setKeep] = useState("");
  const [change, setChange] = useState("");
  const [next, setNext] = useState("");
  const [endedId, setEndedId] = useState<string | null>(null);

  const journey = lookup.state === "ready" ? lookup.journey : null;
  if (journey && !seeded) {
    setSeeded(true);
    setKeep(journey.retro?.keep ?? "");
    setChange(journey.retro?.change ?? "");
    setNext(journey.retro?.next ?? "");
    setEndedId(
      journey.outcome === "in_progress" ? endedAtStageId(journey) : journey.ended_at_stage_id,
    );
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
  if (!journey) {
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

  const j: Journey = journey;
  const now = new Date();
  const closed = j.outcome !== "in_progress";
  const proposed = params.get("outcome");
  const outcome: JourneyOutcome = closed
    ? j.outcome
    : OUTCOMES.find((o) => o === proposed) ?? j.outcome;
  // A live journey reached without a valid ?outcome (a bookmark, a shared URL,
  // the sign-in bounce) has nothing to close INTO: closing on `in_progress`
  // would write an ended_at_stage_id onto a journey that is still open.
  const unresolved = !closed && outcome === "in_progress";
  const offer = outcome === "offer";
  const copy = RETRO_COPY[outcome];
  const text: Record<PromptKey, string> = { keep, change, next };
  const setText: Record<PromptKey, (v: string) => void> = {
    keep: setKeep,
    change: setChange,
    next: setNext,
  };
  const evidence = checkInEvidence(j);
  // The map previews the outcome being proposed, not only the stored one.
  const preview: Journey = { ...j, outcome, ended_at_stage_id: endedId };

  const close = (retro: { keep: string; change: string; next: string } | null) =>
    save.mutate(closeJourney(j, outcome, endedId, retro), {
      onSuccess: () => router.push("/app/journeys"),
    });

  return (
    <Shell>
      <div
        className="relative overflow-hidden rounded-[22px] border px-6 py-[18px]"
        style={{
          background: offer ? "#f6faf3" : "var(--surface-warm)",
          borderColor: offer ? "#cfe0c8" : "var(--border-warm-hair)",
          boxShadow: CARD.boxShadow,
        }}
      >
        {offer &&
          SPECKS.map((speck, i) => (
            <span key={i} aria-hidden="true" style={{ position: "absolute", ...speck }} />
          ))}
        <div className="flex flex-wrap items-center gap-[13px]">
          <CompanyTile initial={initial(j.company)} hue={tileHue(j.company)} />
          <div className="min-w-0 flex-1">
            <h1
              className="text-[26px] font-normal leading-[1.15]"
              style={{ fontFamily: SERIF, color: "var(--ink)" }}
            >
              {j.role} · {j.company}
            </h1>
            <div className="mt-[5px] text-[12.5px]" style={{ color: "var(--ink-4)" }}>
              {retroSummary(preview, now)}
            </div>
          </div>
          {offer ? (
            <span
              className="h-popin rounded-full px-[13px] py-[5px] text-[11px] font-bold tracking-[0.06em]"
              style={{ background: "var(--sage)", color: "#f6faf3" }}
            >
              {copy.pill}
            </span>
          ) : (
            <Pill tone="muted">{copy.pill}</Pill>
          )}
          {closed && (
            <span
              className="ml-auto text-[12px]"
              style={{ color: offer ? "var(--sage)" : "#a3927f" }}
            >
              {`closed ${shortDate(j.updated_at)} · ${j.retro ? "retro saved" : "no retro yet"}`}
            </span>
          )}
        </div>
      </div>

      {/* The track must not be a flex item: its connectors are flex:1 and
          would shrink to zero. Give it a min-width-0 wrapper that grows. */}
      <section
        className="mt-[18px] rounded-[18px] border px-6 py-5"
        style={{ background: "#fdf7ee", borderColor: "var(--border-warm-hair)" }}
      >
        <div className="flex items-start overflow-x-auto">
          <div className="min-w-0 flex-1">
            <JourneyTrack stages={retroTrack(preview, now)} layout="fluid" />
          </div>
        </div>
      </section>

      {evidence.length > 0 && (
        <section
          className="mt-[18px] rounded-[16px] border px-5 py-4"
          style={{ background: "var(--terracotta-tint)", borderColor: "var(--border-warm)" }}
        >
          <MonoLabel color="var(--terracotta-d)">From your check-ins along the way</MonoLabel>
          <div className="mt-2.5 flex flex-wrap gap-2">
            {evidence.map((e, i) => (
              <span
                key={`${e.stageId}-${i}`}
                className="rounded-[10px] border px-3 py-1.5 text-[12px]"
                style={{
                  background: "var(--surface-warm)",
                  borderColor: "var(--border-warm)",
                  color: "var(--ink-2)",
                }}
              >
                {e.stageName} — <b>{e.text}</b>
              </span>
            ))}
          </div>
        </section>
      )}

      <div className="mt-[18px] grid gap-3.5 md:grid-cols-3">
        {PROMPT_KEYS.map((k) => (
          <div key={k} className="rounded-[16px] border p-[18px]" style={PROMPT[k].panel}>
            <MonoLabel color={PROMPT[k].label}>{copy[k]}</MonoLabel>
            <textarea
              rows={4}
              aria-label={copy[k]}
              value={text[k]}
              onChange={(e) => setText[k](e.target.value)}
              className="wm-input mt-2.5 w-full rounded-[10px] p-2.5 text-[13px] leading-[1.65] outline-none"
            />
          </div>
        ))}
      </div>

      {!offer && (
        <label
          className="mt-4 flex flex-wrap items-center gap-2.5 text-[12.5px]"
          style={{ color: "var(--ink-4)" }}
        >
          Ended at
          <select
            className="wm-input h-[30px] rounded-[9px] px-2 text-[12.5px] outline-none"
            value={endedId ?? ""}
            onChange={(e) => setEndedId(e.target.value || null)}
          >
            <option value="">—</option>
            {j.stages.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
      )}

      <div className="mt-5 flex flex-wrap items-center gap-3.5">
        <button
          className="wm-cta h-11 rounded-[12px] px-[22px] text-[13.5px] font-semibold disabled:opacity-40"
          disabled={save.isPending || unresolved}
          onClick={() => close({ keep, change, next })}
        >
          {closed ? "Save the retro" : "Close this journey"}
        </button>
        {!closed && !unresolved && (
          <button className="wm-nav-quiet text-[12.5px]" onClick={() => close(null)}>
            Just close it — no retro
          </button>
        )}
        {unresolved && (
          <span className="text-[12.5px]" style={{ color: "var(--ink-4)" }}>
            This journey is still open — close it from the board with Offer, Didn&apos;t move on
            or Withdrew.
          </span>
        )}
        <span className="text-[12.5px]" style={{ color: "var(--ink-4)" }}>
          We&apos;ll put “next time, do this” in your prep list before the next round like this
          one.
        </span>
      </div>
      {save.isError && (
        <p className="mt-2 text-[11.5px]" style={{ color: "var(--brick)" }}>
          {`Couldn't save that — ${String(save.error)}`}
        </p>
      )}
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
