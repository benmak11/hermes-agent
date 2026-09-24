// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/**
 * Pure edit-side derivations for the loop builder (Warm Flow screen 17) and
 * the stage detail page (screen 18). The board's read-side derivations live
 * in `journeysDerive.ts`; this is the half that *builds* stage arrays, so it
 * stays separate and that file stays byte-identical.
 *
 * No React, no network, no storage. `newId` is injectable so the tests are
 * deterministic, and `now` is always passed in — never read here.
 *
 * The rule that matters: a loop rebuild may never silently drop a stage that
 * carries history. `draftToStages` re-adds anything with a check-in, prep
 * items or questions, whatever the UI did.
 */

import type { TrackStage } from "@/components/warm/journeyStages";
import type { Fmt } from "@/lib/journeysDerive";
import { APPLIED_STAGE_ID, advanceStages, currentStage, stagesToTrack } from "@/lib/journeysDerive";
import type { CheckIn, Journey, JourneyOutcome, JourneyStage, PrepItem } from "@/lib/types";

export type LoopTemplate = { id: string; label: string; stages: string[] };

/** The counts in the UI are `t.stages.length`, so they cannot drift. */
export const LOOP_TEMPLATES: LoopTemplate[] = [
  {
    id: "engineering",
    label: "Standard engineering loop",
    stages: ["Recruiter call", "Technical screen", "Onsite loop", "Decision"],
  },
  {
    id: "takehome",
    label: "Take-home first",
    stages: ["Take-home", "Technical review", "Hiring manager"],
  },
  {
    id: "staff",
    label: "Staff / leadership loop",
    stages: [
      "Recruiter call",
      "Hiring manager",
      "System design",
      "Values conversation",
      "Decision",
    ],
  },
  { id: "scratch", label: "Build it from scratch", stages: [] },
];

const uuid = (): string => crypto.randomUUID();

export function emptyStage(name: string, newId: () => string = uuid): JourneyStage {
  return {
    id: newId(),
    name,
    status: "upcoming",
    scheduled_at: null,
    format: null,
    who: null,
    checkin: null,
    questions: [],
    prep: [],
  };
}

/** Does this stage carry anything the user would lose if it were dropped? */
export function hasData(s: JourneyStage): boolean {
  return (
    s.status === "done" || s.checkin !== null || s.prep.length > 0 || s.questions.length > 0
  );
}

/** A live journey with nothing booked ahead — the board's "rest of the loop
 *  unknown" node, and the reason the builder exists. */
export function loopUnknown(j: Journey): boolean {
  return j.outcome === "in_progress" && !j.stages.some((s) => s.status === "upcoming");
}

const key = (name: string): string => name.trim().toLowerCase();

/**
 * Apply a template against the *stored* stages: a template name that matches
 * a stored stage (case/space-insensitive, first unconsumed match wins) reuses
 * that stage object whole — id, check-in, prep, questions, date, and its own
 * spelling of the name. Stored stages the template didn't name are dropped
 * unless they carry data, in which case they are re-added at the front in
 * their original order. Statuses are then normalised.
 */
export function buildStagesFromTemplate(
  template: string[],
  existing: JourneyStage[],
  newId: () => string = uuid,
): JourneyStage[] {
  const consumed = new Set<string>();
  const picked = template.map((name) => {
    const match = existing.find((s) => !consumed.has(s.id) && key(s.name) === key(name));
    if (!match) return emptyStage(name, newId);
    consumed.add(match.id);
    return match;
  });
  // Carried stages keep their place relative to the template: finished ones lead
  // (so normalizeStatuses can't demote them back to `upcoming`), unfinished ones
  // trail. Without this, applying a template can strip a checked-in stage's ✓.
  const carried = existing.filter((s) => !consumed.has(s.id) && hasData(s));
  const carriedDone = carried.filter((s) => s.status === "done");
  const carriedRest = carried.filter((s) => s.status !== "done");
  return normalizeStatuses([...carriedDone, ...picked, ...carriedRest]);
}

/**
 * The draft the user confirms → the stages that get PUT. Blank-named rows are
 * dropped; any stored stage with data the draft no longer contains is put
 * back at the front. This is the guard that survives a UI bug.
 */
export function draftToStages(
  draft: JourneyStage[],
  existing: JourneyStage[],
  newId: () => string = uuid,
): JourneyStage[] {
  const kept = draft
    .filter((s) => s.name.trim() !== "")
    .map((s) => (s.id ? s : { ...s, id: newId() }));
  const keptIds = new Set(kept.map((s) => s.id));
  const restored = existing.filter((s) => hasData(s) && !keptIds.has(s.id));
  return normalizeStatuses([...restored, ...kept]);
}

/**
 * Linear stage model, same as `advanceStages`: everything up to the first
 * not-done stage is done, that one is current, the rest upcoming. Exactly one
 * `current` — or none when every stage is done — so the server's invariant
 * (`models/journey.py:79-86`) can never 422 from the builder.
 */
export function normalizeStatuses(stages: JourneyStage[]): JourneyStage[] {
  const k = stages.findIndex((s) => s.status !== "done");
  return stages.map((s, i) => ({
    ...s,
    status: k < 0 || i < k ? "done" : i === k ? "current" : "upcoming",
  }));
}

const IMPROVE = "To improve: ";

function improvements(sentence: string): string[] {
  return sentence
    .split(" · ")
    .filter((part) => part.startsWith(IMPROVE))
    .map((part) => part.slice(IMPROVE.length));
}

/**
 * Prep suggestions drawn from the user's *other* journeys, newest first:
 * `retro.change`, then `retro.next`, then the "To improve: …" halves of any
 * check-in sentence (the byte-exact prefix the legacy importer writes,
 * `journeysDerive.ts:321`). Nothing already on this stage's checklist, and no
 * duplicate text, comes back. `closeJourney` (below) is what writes
 * `retro.change`/`retro.next`, so the retro's closing promise is literal.
 *
 * Suggestions only — the caller renders them and writes nothing until a click.
 */
export function seedPrep(
  journeys: Journey[],
  journeyId: string,
  stage: JourneyStage,
  limit = 3,
): PrepItem[] {
  const seen = new Set(stage.prep.map((p) => key(p.text)));
  const out: PrepItem[] = [];
  const others = journeys
    .filter((j) => j.id !== journeyId)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  for (const j of others) {
    const texts = [
      j.retro?.change ?? "",
      j.retro?.next ?? "",
      ...j.stages.flatMap((s) => (s.checkin?.sentence ? improvements(s.checkin.sentence) : [])),
    ];
    for (const raw of texts) {
      const text = raw.trim();
      if (!text || seen.has(key(text))) continue;
      seen.add(key(text));
      out.push({ text, done: false, from_journey_id: j.id });
    }
  }
  return out.slice(0, limit);
}

// ---- <input type="datetime-local"> ----

/** `""` or wall-clock `"2026-09-20T14:00"`. Built from `Intl` parts, never
 *  `toISOString().slice(0,16)` — that is UTC and the input is local time. */
export function toLocalInput(iso: string | null, fmt?: Fmt): string {
  if (!iso) return "";
  // `en-US` deliberately: its numeric month/day are unpadded, so the padding
  // below is this function's own job rather than a locale accident.
  const parts = new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    hourCycle: "h23",
    timeZone: fmt?.timeZone,
  }).formatToParts(new Date(iso));
  const at = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  const pad = (v: string, n = 2) => v.padStart(n, "0");
  return `${pad(at("year"), 4)}-${pad(at("month"))}-${pad(at("day"))}T${pad(at("hour"))}:${pad(at("minute"))}`;
}

export function fromLocalInput(value: string): string | null {
  if (!value) return null;
  return new Date(value).toISOString();
}

/**
 * "today" / "tomorrow" / "in 6 days" for the stage header pill, or null when
 * the moment has passed or is more than a fortnight out. The delta is a
 * calendar-day delta in `fmt`'s zone, so an evening slot tomorrow morning is
 * "tomorrow", not "in 1 day"-and-a-bit.
 */
export function relativeWhen(iso: string, now: Date, fmt?: Fmt): string | null {
  const at = new Date(iso);
  if (at.getTime() < now.getTime()) return null;
  const days = dayNumber(at, fmt) - dayNumber(now, fmt);
  if (days < 0) return null;
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days < 14) return `in ${days} days`;
  return null;
}

const DAY_MS = 24 * 60 * 60 * 1000;

function dayNumber(d: Date, fmt?: Fmt): number {
  const parts = new Intl.DateTimeFormat("en-GB", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    timeZone: fmt?.timeZone,
  }).formatToParts(d);
  const at = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return Date.UTC(at("year"), at("month") - 1, at("day")) / DAY_MS;
}

// ---- check-in + closing a journey (Warm Flow screens 19-20) ----

/** The six the design offers, in its order. Free text either way: a tag the
 *  user types lands in the same `CheckIn.tags` array and is indistinguishable
 *  downstream. */
export const CHECKIN_TAGS: readonly string[] = [
  "Ran out of time",
  "Froze on a question",
  "Went too deep too early",
  "Didn't ask enough back",
  "Good rapport",
  "Told my story well",
];

/** The form's words. Deliberately a different register from `checkinNote`
 *  (`journeysDerive.ts:102`): the form asks how it felt, the board reports it. */
export const RATING_LABELS: Record<1 | 2 | 3 | 4 | 5, string> = {
  1: "Rough",
  2: "Shaky",
  3: "Okay",
  4: "Good",
  5: "Great",
};

/** Screen 20's labels per outcome, as data so a test can cover the copy.
 *  `in_progress` is unreachable from a closed journey but reachable from the
 *  retro of a live one; it reuses the rejected set. */
export const RETRO_COPY: Record<
  JourneyOutcome,
  { pill: string; keep: string; change: string; next: string }
> = {
  offer: {
    pill: "OFFER 🎉",
    keep: "What carried it",
    change: "What was hardest",
    next: "Next time, do this",
  },
  rejected: {
    pill: "didn't move on",
    keep: "What you'd keep",
    change: "What cost you",
    next: "Next time, do this",
  },
  withdrawn: {
    pill: "withdrew",
    keep: "What you'd keep",
    change: "Why you stepped away",
    next: "Next time, do this",
  },
  in_progress: {
    pill: "still going",
    keep: "What you'd keep",
    change: "What cost you",
    next: "Next time, do this",
  },
};

/**
 * Board "✓ Done with X". Advances only when the stage is the current one —
 * `advanceStages` on an already-done stage would move the marker BACK and
 * demote every later done stage. `needsCheckIn` is false for the Applied
 * stage and for a stage that already carries a check-in.
 */
export function markDone(
  j: Journey,
  stageId: string,
): { journey: Journey; needsCheckIn: boolean } {
  const stage = j.stages.find((s) => s.id === stageId);
  if (!stage) return { journey: j, needsCheckIn: false };
  const journey =
    stage.status === "current" ? { ...j, stages: advanceStages(j.stages, stageId) } : j;
  return {
    journey,
    needsCheckIn: stage.checkin === null && stage.id !== APPLIED_STAGE_ID,
  };
}

/**
 * Writes the check-in on exactly one stage. `null` (an empty form, or "skip")
 * leaves `checkin: null` — never `{}`, which `thisWeek` would read as
 * "checked in" and drop from the waiting count. Also completes the stage when
 * it is still `current`, so the stage-page entry point is one PUT, not two.
 */
export function applyCheckIn(j: Journey, stageId: string, checkin: CheckIn | null): Journey {
  const stage = j.stages.find((s) => s.id === stageId);
  if (!stage) return j;
  const stages = stage.status === "current" ? advanceStages(j.stages, stageId) : j.stages;
  return { ...j, stages: stages.map((s) => (s.id === stageId ? { ...s, checkin } : s)) };
}

/** = `applyCheckIn(j, stageId, null)`. Exists so the invariant has a name:
 *  skipping writes no check-in object at all.
 *
 *  **No production caller, deliberately.** The spec gave it one job — skipping
 *  from the stage page while the stage is still `current` should still mark it
 *  done — but the user never said the stage was over, so the Skip button
 *  navigates away and writes nothing at all. Kept as the executable statement
 *  of "a skip is `null`, never `{}`"; the test is its only caller. */
export function skipCheckIn(j: Journey, stageId: string): Journey {
  return applyCheckIn(j, stageId, null);
}

/**
 * The form → storage. Returns `null` when nothing was entered — no rating, no
 * tags, a blank sentence. This is the only constructor of a `CheckIn`, and it
 * is what keeps an all-null object out of Firestore.
 */
export function buildCheckIn(
  rating: number | null,
  tags: string[],
  sentence: string,
  nowIso: string,
): CheckIn | null {
  const kept = tags.map((t) => t.trim()).filter((t) => t !== "");
  const text = sentence.trim();
  if (rating === null && kept.length === 0 && text === "") return null;
  return { rating, tags: kept, sentence: text || null, at: nowIso };
}

/**
 * One PUT closes the journey. `ended_at_stage_id` is forced to null for an
 * offer (an offer did not end at a stage) and validated against `j.stages`
 * otherwise, because a later loop rebuild can delete the stage it pointed at.
 * An all-blank retro is stored as `null`, not three nulls.
 */
export function closeJourney(
  j: Journey,
  outcome: JourneyOutcome,
  endedAtStageId: string | null,
  retro: { keep: string; change: string; next: string } | null,
): Journey {
  const ended =
    outcome === "offer" || !endedAtStageId || !j.stages.some((s) => s.id === endedAtStageId)
      ? null
      : endedAtStageId;
  const keep = retro?.keep.trim() ?? "";
  const change = retro?.change.trim() ?? "";
  const next = retro?.next.trim() ?? "";
  const any = keep !== "" || change !== "" || next !== "";
  return {
    ...j,
    outcome,
    ended_at_stage_id: ended,
    retro: any
      ? { keep: keep || null, change: change || null, next: next || null }
      : null,
  };
}

/** The board's inline "where did this stop" expression, named: the current
 *  stage, else the last done one, else nothing. */
export function endedAtStageId(j: Journey): string | null {
  const cur = currentStage(j);
  if (cur) return cur.id;
  return [...j.stages].reverse().find((s) => s.status === "done")?.id ?? null;
}

/** Screen 20's evidence chips: every tag, then the sentence, per checked-in
 *  stage in stage order. Applied never has one and is skipped anyway. */
export function checkInEvidence(
  j: Journey,
): { stageId: string; stageName: string; text: string }[] {
  const out: { stageId: string; stageName: string; text: string }[] = [];
  for (const s of j.stages) {
    if (s.id === APPLIED_STAGE_ID || !s.checkin) continue;
    for (const tag of s.checkin.tags) {
      out.push({ stageId: s.id, stageName: s.name, text: tag });
    }
    const sentence = s.checkin.sentence?.trim();
    if (sentence) out.push({ stageId: s.id, stageName: s.name, text: sentence });
  }
  return out;
}

/**
 * The week strip's "N check-ins waiting", as the list behind the count. The
 * predicate is deliberately identical to `thisWeek`'s (`journeysDerive.ts:183`)
 * — that file has to stay byte-identical, so the two are pinned to the same
 * number by a test instead of shared code.
 */
export function pendingCheckIns(
  journeys: Journey[],
): { journeyId: string; stageId: string; stageName: string; company: string }[] {
  const out: { journeyId: string; stageId: string; stageName: string; company: string }[] = [];
  for (const j of journeys) {
    if (j.outcome !== "in_progress") continue;
    for (const s of j.stages) {
      if (s.status === "done" && s.checkin === null && s.id !== APPLIED_STAGE_ID) {
        out.push({ journeyId: j.id, stageId: s.id, stageName: s.name, company: j.company });
      }
    }
  }
  return out;
}

/**
 * Screen 20's replayed map: `stagesToTrack` as the board draws it, then the
 * brick ✕ on the stage the process stopped at and "never happened" on the
 * ones after it that never ran. An offer (or a still-live journey) is marked
 * nowhere — the arc reads as all-done. `journeysDerive.ts` stays untouched.
 */
export function retroTrack(j: Journey, now: Date, fmt?: Fmt): TrackStage[] {
  const track = stagesToTrack(j, now, fmt);
  if (j.outcome === "offer" || j.outcome === "in_progress") return track;
  const endedIdx = track.findIndex((t) => t.id === j.ended_at_stage_id);
  if (endedIdx < 0) return track;
  return track.map((t, i) => {
    if (i === endedIdx) return { ...t, ended: true };
    const s = j.stages[i];
    if (i > endedIdx && s.status !== "done" && s.checkin === null) {
      return { ...t, note: "never happened", noteTone: "muted" as const };
    }
    return t;
  });
}

/** "6 weeks · 4 stages · ended at Technical" (an offer: "· all stages done").
 *  Both halves are derived — nothing about a journey's length is stored. */
export function retroSummary(j: Journey, now: Date): string {
  const days = Math.floor((now.getTime() - new Date(j.created_at).getTime()) / DAY_MS);
  const weeks = Math.max(1, Math.floor(days / 7));
  const n = j.stages.length;
  const tail =
    j.outcome === "offer"
      ? "all stages done"
      : `ended at ${j.stages.find((s) => s.id === j.ended_at_stage_id)?.name ?? "—"}`;
  return `${weeks} ${weeks === 1 ? "week" : "weeks"} · ${n} ${n === 1 ? "stage" : "stages"} · ${tail}`;
}
