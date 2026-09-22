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

import type { Fmt } from "@/lib/journeysDerive";
import type { Journey, JourneyStage, PrepItem } from "@/lib/types";

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
 * duplicate text, comes back. Thin until PR 11 gives `retro.change` a writer.
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
