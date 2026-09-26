// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/**
 * Pure derivations for the insights screen (Warm Flow screen 21).
 *
 * The rule this file exists to enforce: **no LLM, no scoring, no opinion** —
 * every number here is a count of something the user typed, and every string
 * it returns is either the user's own words or a count of them. Nothing
 * reads the clock, the network, `crypto` or `window`: `now` and `fmt` are
 * always parameters, so the tests pin them.
 *
 * The second rule is `MIN_SAMPLE`: below three observations a proportion is
 * not a rate, and `barTone` refuses to hand out a verdict colour. No
 * percentage number is rendered anywhere on the page at any N — the bar's
 * *width* is the only place a ratio appears at all.
 *
 * Counting is "settled only": a live journey's `current` stage is neither
 * passed nor failed, so it counts nowhere. Counting it would turn "I have an
 * onsite next Tuesday" into "0 of 1 got past System design".
 */

import { LOOP_TEMPLATES, improvements } from "@/lib/journeyEdit";
import type { Fmt } from "@/lib/journeysDerive";
import { APPLIED_STAGE_ID, shortDate } from "@/lib/journeysDerive";
import type { Journey, JourneyStage } from "@/lib/types";

/** Same normaliser as `journeyEdit.ts:80` — case/space-insensitive names. */
const key = (name: string): string => name.trim().toLowerCase();

/**
 * Below this, a proportion is not a rate — it is two anecdotes with a
 * denominator. Nothing on this page draws a bar-with-a-verdict, or a
 * sage/brick tint, on fewer observations than this.
 */
export const MIN_SAMPLE = 3;

export function enoughToCall(denominator: number): boolean {
  return denominator >= MIN_SAMPLE;
}

// ---- header ----

export type InsightsHeader = { journeys: number; checkIns: number; since: string | null };

/** `since` is the *oldest* `created_at`: the page counts everything the user
 *  has, so labelling it with a window it doesn't apply would be a false
 *  claim about the data. */
export function insightsHeader(journeys: Journey[], fmt?: Fmt): InsightsHeader {
  const checkIns = journeys.reduce(
    (n, j) => n + j.stages.filter((s) => s.checkin !== null).length,
    0,
  );
  let oldest: string | null = null;
  for (const j of journeys) {
    if (oldest === null || j.created_at < oldest) oldest = j.created_at;
  }
  return {
    journeys: journeys.length,
    checkIns,
    since: oldest === null ? null : shortDate(oldest, fmt),
  };
}

// ---- band 1: where your journeys end ----

export type StageOutcomeRow = {
  key: string;
  name: string;
  reached: number;
  gotPast: number;
  order: number;
};

type Settled = { stage: JourneyStage; index: number; gotPast: boolean };

/**
 * The stages of one journey that are *settled*, which is what band 1 counts.
 * A stage counts when it is `done` (and is not the stage the journey ended
 * at), or when it *is* the stage a closed journey ended at. Everything else —
 * `upcoming`, the `current` stage of a live journey, and the never-happened
 * tail of a closed journey — counts nowhere.
 *
 * Band 3 deliberately does *not* go through here: `bestLoopTemplate` matches
 * over every stage name, settled or not, which is what lets a freshly built
 * loop appear at all. Those journeys are counted as `live` and `countLine`
 * labels them "still going", so nothing unfinished is read as an outcome.
 */
function settledStages(j: Journey): Settled[] {
  const closed = j.outcome === "rejected" || j.outcome === "withdrawn";
  const endedIdx = closed ? j.stages.findIndex((s) => s.id === j.ended_at_stage_id) : -1;
  const out: Settled[] = [];
  j.stages.forEach((stage, index) => {
    if (index === endedIdx) out.push({ stage, index, gotPast: false });
    else if (stage.status === "done") out.push({ stage, index, gotPast: true });
  });
  return out;
}

/**
 * §2.1. One row per distinct stage name, grouped case-insensitively and
 * labelled with the commonest original spelling (ties by first seen). No
 * semantic clustering: "Technical screen" and "Technical / coding" stay two
 * rows, because merging them is a judgement call and this screen has none.
 * `applied` is excluded — every journey has it, so it would be a permanent
 * 100% row that means nothing. Rows are ordered by their mean position in
 * the loops they appear in, so they read in loop order.
 */
export function endStageRows(journeys: Journey[]): StageOutcomeRow[] {
  type Acc = {
    key: string;
    spellings: Map<string, number>;
    reached: number;
    gotPast: number;
    indexSum: number;
  };
  const acc = new Map<string, Acc>();
  for (const j of journeys) {
    for (const { stage, index, gotPast } of settledStages(j)) {
      if (stage.id === APPLIED_STAGE_ID) continue;
      const k = key(stage.name);
      if (k === "") continue;
      let row = acc.get(k);
      if (!row) {
        row = { key: k, spellings: new Map(), reached: 0, gotPast: 0, indexSum: 0 };
        acc.set(k, row);
      }
      const spelling = stage.name.trim();
      row.spellings.set(spelling, (row.spellings.get(spelling) ?? 0) + 1);
      row.reached += 1;
      row.gotPast += gotPast ? 1 : 0;
      row.indexSum += index;
    }
  }
  const rows: StageOutcomeRow[] = [...acc.values()].map((row) => {
    let name = "";
    let best = 0;
    for (const [spelling, n] of row.spellings) {
      if (n > best) {
        best = n;
        name = spelling;
      }
    }
    return {
      key: row.key,
      name,
      reached: row.reached,
      gotPast: row.gotPast,
      order: row.indexSum / row.reached,
    };
  });
  return rows.sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
}

export type BarTone = "good" | "warn" | "bad" | "unknown";

/** The verdict colour, and the gate on it. The 1.0 / 0.4 thresholds are read
 *  back from the design's own rows (100% sage, 40% honey, 33% brick). */
export function barTone(row: StageOutcomeRow): BarTone {
  if (!enoughToCall(row.reached)) return "unknown";
  const ratio = row.gotPast / row.reached;
  if (ratio === 1) return "good";
  if (ratio >= 0.4) return "warn";
  return "bad";
}

export type TagCount = { tag: string; count: number; ofCheckIns: number };

/** The most-used check-in tag, or `null` below two uses — a tag picked once
 *  is not a pattern, and calling it one would be the screen's whole failure
 *  mode in miniature. */
export function topTag(journeys: Journey[]): TagCount | null {
  const counts = new Map<string, number>();
  let ofCheckIns = 0;
  for (const j of journeys) {
    for (const s of j.stages) {
      if (!s.checkin) continue;
      ofCheckIns += 1;
      for (const raw of s.checkin.tags) {
        const tag = raw.trim();
        if (!tag) continue;
        counts.set(tag, (counts.get(tag) ?? 0) + 1);
      }
    }
  }
  let top: TagCount | null = null;
  for (const [tag, count] of counts) {
    if (top === null || count > top.count) top = { tag, count, ofCheckIns };
  }
  if (top === null || top.count < 2) return null;
  return { ...top, ofCheckIns };
}

// ---- band 2: take this into the next one ----

export type CarryItem = { text: string; note: string; journeyIds: string[] };

/** The byte-exact shape the legacy importer writes (`journeysDerive.ts:319-322`). */
const LEGACY_NOTE = /(^|· )(Went well: |To improve: )/;

type Candidate = { text: string; from: "retro" | "checkin"; stageName: string };

/** The texts one journey contributes, strongest first: "next time, do this",
 *  then "what I'd change", then the check-in sentences. A legacy-format
 *  sentence contributes only its "To improve: " halves — the "Went well: "
 *  half is not something to carry forward. */
function candidates(j: Journey): Candidate[] {
  const out: Candidate[] = [
    { text: j.retro?.next ?? "", from: "retro", stageName: "" },
    { text: j.retro?.change ?? "", from: "retro", stageName: "" },
  ];
  for (const s of j.stages) {
    const sentence = s.checkin?.sentence?.trim() ?? "";
    if (!sentence) continue;
    const texts = LEGACY_NOTE.test(sentence) ? improvements(sentence) : [sentence];
    for (const text of texts) out.push({ text, from: "checkin", stageName: s.name });
  }
  return out;
}

/**
 * §2.2. Every candidate text the user wrote, deduped case-insensitively and
 * attributed to where it came from. Most-repeated first (a note written
 * twice is the real pattern), then newest journey first. Nothing is invented:
 * the note beside each item names a company and a place the user can go and
 * check.
 */
export function carryForward(journeys: Journey[], limit = 3): CarryItem[] {
  // A text already on a *live* journey's prep list is already doing its job;
  // say so rather than repeating it as new advice.
  const prepped = new Map<string, string>();
  for (const j of journeys) {
    if (j.outcome !== "in_progress") continue;
    for (const s of j.stages) {
      for (const p of s.prep) {
        const k = key(p.text);
        if (k && !prepped.has(k)) prepped.set(k, j.company);
      }
    }
  }

  type Item = {
    text: string;
    from: "retro" | "checkin";
    stageName: string;
    companies: string[];
    journeyIds: string[];
    updatedAt: string;
  };
  const items = new Map<string, Item>();
  const ordered = [...journeys].sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  for (const j of ordered) {
    for (const c of candidates(j)) {
      const text = c.text.trim();
      if (!text) continue;
      const k = key(text);
      const existing = items.get(k);
      if (!existing) {
        items.set(k, {
          text,
          from: c.from,
          stageName: c.stageName,
          companies: [j.company],
          journeyIds: [j.id],
          updatedAt: j.updated_at,
        });
      } else if (!existing.journeyIds.includes(j.id)) {
        // The journey id is the repeat signal (it drives the ordering); the
        // company list is what gets *named*, so it is deduped — two journeys
        // at one employer must never render "after Stripe and Stripe".
        existing.journeyIds.push(j.id);
        if (!existing.companies.includes(j.company)) existing.companies.push(j.company);
      }
    }
  }

  return [...items.values()]
    .sort(
      (a, b) =>
        b.journeyIds.length - a.journeyIds.length || b.updatedAt.localeCompare(a.updatedAt),
    )
    .slice(0, limit)
    .map((item) => ({
      text: item.text,
      note: attribution(item, prepped),
      journeyIds: item.journeyIds,
    }));
}

function attribution(
  item: {
    text: string;
    from: "retro" | "checkin";
    stageName: string;
    companies: string[];
  },
  prepped: Map<string, string>,
): string {
  // Branching on the deduped company list, not on the journey count: a text
  // written twice at the same employer falls through to the note below, which
  // names that employer once and truthfully.
  const [a, b] = item.companies;
  if (item.companies.length === 2) return `You wrote this after ${a} and ${b}.`;
  if (item.companies.length > 2) {
    return `You wrote this after ${a}, ${b} and ${item.companies.length - 2} more.`;
  }
  const onPrep = prepped.get(key(item.text));
  if (onPrep) return `Already on your ${onPrep} prep list.`;
  if (item.from === "retro") return `From your ${a} retro.`;
  return `From your ${item.stageName} check-in at ${a}.`;
}

// ---- band 3: the loops you've walked ----

export type LoopShapeRow = {
  id: string;
  label: string;
  journeys: number;
  offers: number;
  closed: number;
  live: number;
};

/**
 * Which `LOOP_TEMPLATES` entry a journey's stage names look most like —
 * mechanical set comparison over a vocabulary the user picked from in the
 * loop builder, not a classifier anyone invented here. Two matching names
 * are the floor; one is a coincidence. `scratch` has no stages and is
 * skipped. Which template a journey *actually* used is not stored (see the
 * PR text's deferred follow-up), so this is a stand-in and is labelled as one.
 */
function bestLoopTemplate(j: Journey): string | null {
  const stageKeys = j.stages.map((s) => key(s.name));
  let bestId: string | null = null;
  let bestScore = 0;
  let bestSize = 0;
  for (const t of LOOP_TEMPLATES) {
    if (t.stages.length === 0) continue;
    const used = new Set<number>();
    let score = 0;
    for (const name of t.stages) {
      const i = stageKeys.findIndex((k, idx) => !used.has(idx) && k === key(name));
      if (i >= 0) {
        used.add(i);
        score += 1;
      }
    }
    if (score < 2) continue;
    if (score > bestScore || (score === bestScore && t.stages.length < bestSize)) {
      bestId = t.id;
      bestScore = score;
      bestSize = t.stages.length;
    }
  }
  return bestId;
}

/**
 * §2.3. One journey contributes to at most one row, so the rows never sum to
 * more than the journeys they describe. Counts only — a ratio at N=2 is the
 * exact lie this screen must not tell.
 */
export function loopShapes(journeys: Journey[]): LoopShapeRow[] {
  const rows = new Map<string, LoopShapeRow>();
  for (const t of LOOP_TEMPLATES) {
    if (t.stages.length === 0) continue;
    rows.set(t.id, { id: t.id, label: t.label, journeys: 0, offers: 0, closed: 0, live: 0 });
  }
  for (const j of journeys) {
    const id = bestLoopTemplate(j);
    if (id === null) continue;
    const row = rows.get(id);
    if (!row) continue;
    row.journeys += 1;
    if (j.outcome === "offer") row.offers += 1;
    else if (j.outcome === "in_progress") row.live += 1;
    else row.closed += 1;
  }
  return [...rows.values()].filter((r) => r.journeys > 0);
}

/**
 * §2.3's required sub-line. The rows only cover journeys whose stage names
 * look like a template, so without this the header's journey count and the
 * rows' can disagree by any amount with nothing on the page explaining it —
 * on a page whose whole contract is "counts you can check". It lives here,
 * not in the page, precisely because that reconciliation is a claim: the page
 * is the one place nothing in the suite could check the arithmetic.
 */
export function matchNote(total: number, shapes: LoopShapeRow[]): string {
  const matched = shapes.reduce((n, r) => n + r.journeys, 0);
  const rest = total - matched;
  if (rest === 0) return `Matched by stage name across all ${total} of your journeys.`;
  const subject = rest === 1 ? "journey doesn't" : "journeys don't";
  return `Matched by stage name — ${rest} ${subject} look like any of these shapes.`;
}

/** One band-3 row as a sentence. Counts, never a ratio: "2 of 2 reached the
 *  end" is the lie this page must not tell. A journey with no outcome yet is
 *  named as still going rather than folded into either column. */
export function countLine(r: LoopShapeRow): string {
  const head = `${r.journeys} ${r.journeys === 1 ? "journey" : "journeys"}`;
  const offers = `${r.offers} ${r.offers === 1 ? "offer" : "offers"}`;
  const tail = r.live > 0 ? `, ${r.live} still going` : "";
  return `${head} — ${offers}, ${r.closed} closed${tail}`;
}

// ---- footer ----

/** The design's honesty line (110), parameterised. Digits rather than the
 *  design's spelled-out "Five": one fewer thing to get wrong. */
export function sampleNote(journeyCount: number, checkInCount: number): string {
  if (journeyCount < 10) {
    const subject = journeyCount === 1 ? "journey is" : "journeys are";
    return `${journeyCount} ${subject} a small sample — treat these as hints, not verdicts. They sharpen as you log more.`;
  }
  return `Counted from your ${journeyCount} journeys and ${checkInCount} check-ins. Still your own notes, grouped — not a verdict.`;
}
