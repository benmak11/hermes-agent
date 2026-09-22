// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/**
 * Pure derivations for the journey board (Warm Flow screen 16). No React, no
 * network, no storage: everything takes a `Journey` (or the list) plus an
 * explicit `now`, so the page computes `now` once per render and the tests
 * pin it. `stagesToTrack` returns the same `TrackStage[]` the marketing hero
 * feeds `<JourneyTrack>` with — that shared type is the seam between the two.
 */

import type { TrackStage } from "@/components/warm/journeyStages";
import type {
  Application,
  Journey,
  JourneyInput,
  JourneyStage,
  JourneyStageStatus,
} from "@/lib/types";

/** The stage the app creates for "you applied"; never gets a check-in. */
export const APPLIED_STAGE_ID = "applied";
export const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

/** Tests pass `{ timeZone: "UTC" }`; the page passes nothing (local time). */
export type Fmt = { timeZone?: string };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2 Sep". The month table is fixed rather than `month: "short"` because
 *  newer ICU data spells September "Sept" in en-GB. */
export function shortDate(iso: string, fmt?: Fmt): string {
  const parts = new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "numeric",
    timeZone: fmt?.timeZone,
  }).formatToParts(new Date(iso));
  const part = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return `${part("day")} ${MONTHS[part("month") - 1]}`;
}

/** "Thu 10:00" */
export function weekdayTime(iso: string, fmt?: Fmt): string {
  const d = new Date(iso);
  const time = new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: fmt?.timeZone,
  }).format(d);
  return `${weekday(iso, fmt)} ${time}`;
}

/** "Fri" */
export function weekday(iso: string, fmt?: Fmt): string {
  return new Intl.DateTimeFormat("en-GB", {
    weekday: "short",
    timeZone: fmt?.timeZone,
  }).format(new Date(iso));
}

/** `now <= at < now + 7d` — the strip's window; the far edge is exclusive. */
export function withinWeek(iso: string, now: Date): boolean {
  const at = new Date(iso).getTime();
  const t = now.getTime();
  return t <= at && at - t < WEEK_MS;
}

export function currentStage(j: Journey): JourneyStage | undefined {
  return j.stages.find((s) => s.status === "current");
}

/**
 * Linear stage model (ported from the interview journal): clicking a stage
 * moves the "current" marker there — everything before it is done, everything
 * after upcoming. Clicking the current stage completes it and advances, which
 * can leave no current stage at all (every stage done).
 */
export function advanceStages(stages: JourneyStage[], clickedId: string): JourneyStage[] {
  const idx = stages.findIndex((s) => s.id === clickedId);
  if (idx < 0) return stages;
  const clicked = stages[idx];
  const currentIdx =
    clicked.status === "current" ? Math.min(idx + 1, stages.length) : idx;
  return stages.map((s, i) => ({
    ...s,
    status: i < currentIdx ? "done" : i === currentIdx ? "current" : "upcoming",
  }));
}

/** Earliest `scheduled_at >= now` on a stage that is not done, or null. */
export function nextUp(j: Journey, now: Date): { stage: JourneyStage; at: string } | null {
  let best: { stage: JourneyStage; at: string } | null = null;
  for (const stage of j.stages) {
    if (stage.status === "done" || !stage.scheduled_at) continue;
    if (new Date(stage.scheduled_at).getTime() < now.getTime()) continue;
    if (!best || stage.scheduled_at < best.at) best = { stage, at: stage.scheduled_at };
  }
  return best;
}

function checkinNote(rating: number | null): string {
  switch (rating) {
    case 5:
      return "went great";
    case 4:
      return "went well";
    case 3:
      return "felt okay";
    case 2:
      return "felt shaky";
    case 1:
      return "felt rough";
    default:
      return "checked in";
  }
}

function noteFor(
  s: JourneyStage,
  now: Date,
  fmt?: Fmt,
): Pick<TrackStage, "note" | "noteTone"> {
  if (s.status === "done") {
    if (s.checkin) {
      return {
        note: checkinNote(s.checkin.rating),
        noteTone: (s.checkin.rating ?? 0) >= 4 ? "good" : "muted",
      };
    }
    return { note: s.scheduled_at ? shortDate(s.scheduled_at, fmt) : undefined, noteTone: "muted" };
  }
  if (s.status === "current") {
    if (!s.scheduled_at) return { note: "you're here", noteTone: "accent" };
    const when = withinWeek(s.scheduled_at, now)
      ? weekday(s.scheduled_at, fmt)
      : shortDate(s.scheduled_at, fmt);
    return { note: `${when} · you're here`, noteTone: "accent" };
  }
  if (!s.scheduled_at) return { note: "not booked", noteTone: "muted" };
  return {
    note: withinWeek(s.scheduled_at, now)
      ? weekdayTime(s.scheduled_at, fmt)
      : shortDate(s.scheduled_at, fmt),
    noteTone: "muted",
  };
}

/** One journey → the node track. `JourneyStageStatus` is `TrackStatus`. */
export function stagesToTrack(j: Journey, now: Date, fmt?: Fmt): TrackStage[] {
  return j.stages.map((s) => ({
    id: s.id,
    name: s.name,
    status: s.status,
    ...noteFor(s, now, fmt),
  }));
}

export type WeekItem =
  | { kind: "scheduled"; journeyId: string; stageId: string; at: string; label: string }
  | { kind: "checkins"; count: number };

/**
 * The "This week" strip: every stage booked inside the next seven days on a
 * live journey, soonest first, then one item for the done stages still
 * waiting for a check-in (the Applied stage never counts).
 */
export function thisWeek(journeys: Journey[], now: Date): WeekItem[] {
  const live = journeys.filter((j) => j.outcome === "in_progress");
  const scheduled: Extract<WeekItem, { kind: "scheduled" }>[] = [];
  let checkins = 0;
  for (const j of live) {
    for (const s of j.stages) {
      if (s.scheduled_at && withinWeek(s.scheduled_at, now)) {
        scheduled.push({
          kind: "scheduled",
          journeyId: j.id,
          stageId: s.id,
          at: s.scheduled_at,
          label: `${s.name} · ${j.company}`,
        });
      }
      if (s.status === "done" && s.checkin === null && s.id !== APPLIED_STAGE_ID) checkins++;
    }
  }
  scheduled.sort((a, b) => a.at.localeCompare(b.at));
  const items: WeekItem[] = [...scheduled];
  if (checkins > 0) items.push({ kind: "checkins", count: checkins });
  return items;
}

export type BoardSummary = { talking: number; waiting: number; offers: number; closed: number };

/** The counts line under the h1. */
export function boardSummary(journeys: Journey[]): BoardSummary {
  const out: BoardSummary = { talking: 0, waiting: 0, offers: 0, closed: 0 };
  for (const j of journeys) {
    if (j.outcome === "in_progress") {
      if (currentStage(j)) out.talking++;
      else out.waiting++;
    } else if (j.outcome === "offer") out.offers++;
    else out.closed++;
  }
  return out;
}

const OUTCOME_RANK: Record<Journey["outcome"], number> = {
  in_progress: 0,
  offer: 1,
  rejected: 2,
  withdrawn: 2,
};

/** Live first, then offers, then closed; newest activity first within a rank. */
export function sortForBoard(journeys: Journey[]): Journey[] {
  return [...journeys].sort(
    (a, b) =>
      OUTCOME_RANK[a.outcome] - OUTCOME_RANK[b.outcome] ||
      b.updated_at.localeCompare(a.updated_at),
  );
}

function appliedStage(atIso: string): JourneyStage {
  return {
    id: APPLIED_STAGE_ID,
    name: "Applied",
    status: "done",
    scheduled_at: atIso,
    format: null,
    who: null,
    checkin: null,
    questions: [],
    prep: [],
  };
}

function lastTimelineAt(a: Application): string | undefined {
  return a.timeline.length ? a.timeline[a.timeline.length - 1].at : undefined;
}

/** "Track this →" on a submitted application. */
export function applicationToJourneyInput(a: Application, nowIso: string): JourneyInput {
  const at =
    a.confirmation?.submitted_at ?? a.last_submitted_at ?? lastTimelineAt(a) ?? nowIso;
  return {
    company: a.job_company ?? "Unknown",
    role: a.job_title ?? "Untitled role",
    source: "hermes",
    application_id: a.id,
    job_url: a.job_url ?? null,
    stages: [appliedStage(at)],
    outcome: "in_progress",
    ended_at_stage_id: null,
    retro: null,
  };
}

/** "+ Add an opportunity". */
export function manualJourneyInput(company: string, role: string, nowIso: string): JourneyInput {
  return {
    company,
    role,
    source: "manual",
    application_id: null,
    job_url: null,
    stages: [appliedStage(nowIso)],
    outcome: "in_progress",
    ended_at_stage_id: null,
    retro: null,
  };
}

/** Sent applications that no journey tracks yet — the synthesized Applied rows. */
export function untrackedApplications(apps: Application[], journeys: Journey[]): Application[] {
  const tracked = new Set(journeys.map((j) => j.application_id).filter(Boolean));
  return apps.filter(
    (a) => (a.status === "submitted" || a.status === "responded") && !tracked.has(a.id),
  );
}

// ---- legacy import (the localStorage journal, hermes:interviews:v1:{uid}) ----

export type LegacyStage = {
  id: string;
  name: string;
  status: JourneyStageStatus;
  wentWell?: string;
  toImprove?: string;
};
export type LegacyJournalEntry = {
  id: string;
  company: string;
  role: string;
  stages: LegacyStage[];
  sessions: string[];
  outcome: "in_progress" | "offer" | "rejected";
  /** A stage *name*, not an id (the old page stored `cur?.name`). */
  endedAtStage?: string;
  reflection?: string;
  createdAt: number;
};

/** Byte-identical to the deleted `interviews.ts` key. */
export function legacyKey(uid: string): string {
  return `hermes:interviews:v1:${uid}`;
}

export function parseLegacy(raw: string | null): LegacyJournalEntry[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as LegacyJournalEntry[]) : [];
  } catch {
    return [];
  }
}

function legacyStage(s: LegacyStage, nowIso: string): JourneyStage {
  const notes = [
    s.wentWell && `Went well: ${s.wentWell}`,
    s.toImprove && `To improve: ${s.toImprove}`,
  ].filter(Boolean);
  return {
    id: s.id,
    name: s.name,
    status: s.status,
    scheduled_at: null,
    format: null,
    who: null,
    checkin: notes.length
      ? { rating: null, tags: [], sentence: notes.join(" · "), at: nowIso }
      : null,
    questions: [],
    prep: [],
  };
}

/** One journal entry → a POST body. Notes become unrated check-ins, final-round
 *  sessions become done stages, the offer reflection becomes `retro.keep`. */
export function legacyToJourney(e: LegacyJournalEntry, nowIso: string): JourneyInput {
  const stages: JourneyStage[] = e.stages.map((s) => legacyStage(s, nowIso));
  e.sessions.forEach((name, i) => {
    stages.push({
      id: `${e.id}:session:${i}`,
      name,
      status: "done",
      scheduled_at: null,
      format: null,
      who: null,
      checkin: null,
      questions: [],
      prep: [],
    });
  });
  return {
    company: e.company,
    role: e.role,
    source: "manual",
    application_id: null,
    job_url: null,
    stages,
    outcome: e.outcome,
    ended_at_stage_id: stages.find((s) => s.name === e.endedAtStage)?.id ?? null,
    retro: e.reflection ? { keep: e.reflection, change: null, next: null } : null,
  };
}
