// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import Link from "next/link";
import type { CSSProperties } from "react";

import { TopNav } from "@/components/TopNav";
import { MonoLabel } from "@/components/warm/Editable";
import { CARD, SERIF } from "@/components/warm/styles";
import { useAuth } from "@/lib/auth";
import type { BarTone, StageOutcomeRow } from "@/lib/insights";
import {
  barTone,
  carryForward,
  countLine,
  endStageRows,
  insightsHeader,
  loopShapes,
  matchNote,
  sampleNote,
  topTag,
} from "@/lib/insights";
import { useJourneys } from "@/lib/journeys";
import type { Journey } from "@/lib/types";

/**
 * Warm Flow screen 21 — what you're learning. Three bands over the journeys
 * the board already fetched, and not one number that isn't a count of
 * something the user typed: no LLM call, no request of its own, no verdict.
 *
 * Two things the design renders are not derivable from anything stored — the
 * band-1 editorial sentence ("conversations are your strength") and band 3's
 * company/loop *categories* — so they are replaced rather than faked. What is
 * left is the arithmetic in `insights.ts`, which is where the honesty rules
 * live: below `MIN_SAMPLE` observations a row gets no verdict colour and no
 * bar fill, and a percentage number is printed nowhere on this page at any N.
 */

const FILL: Record<Exclude<BarTone, "unknown">, string> = {
  good: "var(--sage)",
  warn: "var(--honey)",
  bad: "var(--terracotta)",
};

const LABEL: Record<BarTone, CSSProperties> = {
  good: { color: "var(--ink-2)", fontWeight: 600 },
  warn: { color: "var(--ink-2)", fontWeight: 600 },
  bad: { color: "var(--brick)", fontWeight: 700 },
  unknown: { color: "var(--ink-4)", fontWeight: 600 },
};

const COUNT: Record<BarTone, string> = {
  good: "var(--sage)",
  warn: "#9a6216",
  bad: "var(--brick)",
  unknown: "#a3927f",
};

/**
 * Band 3's row paint, and there is only one of it. The design tints these
 * rows sage or brick, but any tint here is a verdict on a loop *shape* —
 * over templates the user never recorded choosing (the match is a stand-in
 * for a field nothing stores), and it would contradict its own row:
 * 10 journeys with 1 offer would read "1 offer, 9 closed" in sage. `countLine`
 * already says everything a tint could, and says it in numbers.
 */
const ROW: CSSProperties = { background: "var(--surface-warm)", borderColor: "#e8dacb" };

export default function InsightsPage() {
  const { user, loading } = useAuth();
  const journeysQuery = useJourneys(!!user);

  if (loading || !user || journeysQuery.isPending) {
    return (
      <>
        <TopNav section="journeys" />
        <main className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
          Loading…
        </main>
      </>
    );
  }
  if (journeysQuery.isError || !journeysQuery.data) {
    return (
      <Shell>
        <div
          className="rounded-[18px] border px-6 py-8 text-center text-[13.5px]"
          style={{
            background: "var(--surface-warm)",
            borderColor: "var(--border-warm-hair)",
            color: "var(--brick)",
          }}
        >
          {"Couldn't load your journeys."}
        </div>
      </Shell>
    );
  }

  const journeys: Journey[] = journeysQuery.data.journeys;
  if (journeys.length === 0) {
    return (
      <Shell>
        <Nothing />
      </Shell>
    );
  }

  const hdr = insightsHeader(journeys);
  const rows = endStageRows(journeys);
  const tag = topTag(journeys);
  const carry = carryForward(journeys);
  const shapes = loopShapes(journeys);

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
        <div className="flex flex-wrap items-end justify-between gap-5">
          <div>
            <h1
              className="text-[30px] font-normal leading-[1.15]"
              style={{ fontFamily: SERIF, color: "var(--ink)" }}
            >
              What you&apos;re learning
            </h1>
            <p className="mt-2 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
              {`Across ${hdr.journeys} ${hdr.journeys === 1 ? "journey" : "journeys"} and ${hdr.checkIns} ${hdr.checkIns === 1 ? "stage" : "stages"} you've written about. None of this is a score — it's your own notes, grouped.`}
            </p>
          </div>
          {hdr.since && (
            <span className="text-[12.5px]" style={{ color: "#a3927f" }}>
              since {hdr.since}
            </span>
          )}
        </div>

        <section
          className="mt-[22px] rounded-[18px] border px-6 py-[22px]"
          style={{ background: "#fdf7ee", borderColor: "var(--border-warm-hair)" }}
        >
          <MonoLabel>Where your journeys end</MonoLabel>
          {rows.length === 0 ? (
            <Empty>
              Nothing has finished yet. Once a stage is done — or a journey closes — this is
              where the pattern shows up.
            </Empty>
          ) : (
            <>
              <div className="mt-4 flex flex-col gap-[11px]">
                {rows.map((r) => (
                  <StageBar key={r.key} row={r} />
                ))}
              </div>
              {tag && (
                <p className="mt-4 text-[13px] leading-[1.65]" style={{ color: "var(--ink-2)" }}>
                  “{tag.tag}” is the tag you&apos;ve picked most — in <b>{tag.count}</b> of your{" "}
                  {tag.ofCheckIns} check-ins.
                </p>
              )}
            </>
          )}
        </section>

        <div className="mt-4 grid gap-4 md:grid-cols-[1.15fr_1fr] md:items-start">
          <div
            className="rounded-[18px] border px-6 py-[22px]"
            style={{ background: "var(--terracotta-tint)", borderColor: "var(--border-warm)" }}
          >
            <MonoLabel color="var(--terracotta-d)">Take this into the next one</MonoLabel>
            <p className="mt-2 text-[12.5px]" style={{ color: "var(--ink-4)" }}>
              Pulled from what you wrote, not from advice we made up.
            </p>
            {carry.length === 0 ? (
              <Empty>
                Nothing to carry yet. Write a check-in after a stage, or a retro when a journey
                closes, and your own notes land here.
              </Empty>
            ) : (
              <div className="mt-4 flex flex-col gap-2.5">
                {carry.map((item, i) => (
                  <div
                    key={item.text}
                    className="flex gap-3 rounded-[13px] border px-4 py-3.5"
                    style={{
                      background: "var(--surface-warm)",
                      borderColor: "var(--border-warm)",
                    }}
                  >
                    <span
                      className="flex h-[22px] w-[22px] flex-none items-center justify-center rounded-[7px] text-[12px] font-bold"
                      style={{ background: "var(--terracotta)", color: "#fff9f2" }}
                    >
                      {i + 1}
                    </span>
                    <div>
                      <div className="text-[13.5px] font-bold" style={{ color: "var(--ink)" }}>
                        {item.text}
                      </div>
                      <div className="mt-[5px] text-[12.5px]" style={{ color: "var(--ink-4)" }}>
                        {item.note}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div
            className="rounded-[18px] border px-6 py-[22px]"
            style={{ background: "#fdf7ee", borderColor: "var(--border-warm-hair)" }}
          >
            <MonoLabel>The loops you&apos;ve walked</MonoLabel>
            <p className="mt-2 text-[12.5px]" style={{ color: "var(--ink-4)" }}>
              {matchNote(journeys.length, shapes)}
            </p>
            {shapes.length < 2 ? (
              <Empty>
                One shape so far. This compares loops against each other, so it needs at least
                two.
              </Empty>
            ) : (
              <div className="mt-4 flex flex-col gap-[11px]">
                {shapes.map((r) => (
                  <div
                    key={r.id}
                    className="flex flex-wrap items-center gap-3 rounded-[13px] border px-[15px] py-[13px]"
                    style={ROW}
                  >
                    <span className="flex-1 text-[13px] font-bold" style={{ color: "var(--ink)" }}>
                      {r.label}
                    </span>
                    <span
                      className="text-[12.5px] font-semibold"
                      style={{ color: "var(--ink-4)" }}
                    >
                      {countLine(r)}
                    </span>
                  </div>
                ))}
              </div>
            )}
            <Link
              href="/app/profile"
              className="wm-link mt-3.5 inline-block text-[12.5px] font-semibold"
            >
              Update your match preferences →
            </Link>
          </div>
        </div>

        <p className="mt-[18px] text-[12.5px]" style={{ color: "#a3927f" }}>
          {sampleNote(hdr.journeys, hdr.checkIns)}
        </p>
      </div>
    </Shell>
  );
}

/** Design 45-48. The bar's width is the only ratio on this page, and an
 *  `unknown` row renders no fill element at all — an empty track, rather than
 *  a confident-looking block built from one or two observations. */
function StageBar({ row }: { row: StageOutcomeRow }) {
  const tone = barTone(row);
  const text =
    tone === "unknown"
      ? `${row.gotPast} of ${row.reached} — too few to call`
      : `${row.gotPast} of ${row.reached} got past`;
  return (
    <div className="flex flex-wrap items-center gap-[14px]">
      <span
        className="w-full text-[13px] sm:w-[150px] sm:flex-none"
        style={LABEL[tone]}
      >
        {row.name}
      </span>
      <div
        className="h-[26px] min-w-[80px] flex-1 overflow-hidden rounded-[8px]"
        style={{ background: "#f4ebdf" }}
      >
        {tone !== "unknown" && (
          <div
            style={{
              width: `${Math.round((100 * row.gotPast) / row.reached)}%`,
              height: "100%",
              background: FILL[tone],
            }}
          />
        )}
      </div>
      <span
        className="w-auto text-[12.5px] font-semibold sm:w-[130px] sm:flex-none"
        style={{ color: COUNT[tone] }}
      >
        {text}
      </span>
    </div>
  );
}

/** A band with nothing in it yet keeps its heading and says why — never a
 *  zero, a `0 of 0`, or a bare panel. */
function Empty({ children }: { children: React.ReactNode }) {
  return (
    <p className="mt-4 text-[13px] leading-[1.65]" style={{ color: "var(--ink-4)" }}>
      {children}
    </p>
  );
}

function Nothing() {
  return (
    <div
      className="rounded-[22px] border px-8 py-10 text-center"
      style={{
        background: "var(--surface-warm)",
        borderColor: "var(--border-warm-hair)",
        boxShadow: CARD.boxShadow,
      }}
    >
      <h1
        className="text-[26px] font-normal leading-[1.15]"
        style={{ fontFamily: SERIF, color: "var(--ink)" }}
      >
        Nothing to count yet.
      </h1>
      <p className="mx-auto mt-2.5 max-w-[460px] text-[13.5px]" style={{ color: "var(--ink-4)" }}>
        This page is your own notes, grouped — it fills in as you track journeys and write
        check-ins.
      </p>
      <Link
        href="/app/journeys"
        className="wm-cta mt-5 inline-block rounded-[12px] px-[22px] py-3 text-[13.5px] font-semibold"
      >
        Your journeys →
      </Link>
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <>
      <TopNav section="journeys" />
      <main className="mx-auto w-full max-w-[1180px] flex-1 px-[30px] pt-[26px] pb-7">
        <Link href="/app/journeys" className="wm-nav-quiet mb-3 inline-block text-[12px]">
          ← Your journeys
        </Link>
        {children}
      </main>
    </>
  );
}
