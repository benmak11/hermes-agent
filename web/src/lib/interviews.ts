// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

"use client";

import { useStored, writeStored } from "@/lib/localStore";

/**
 * The interview journal is user-owned and user-logged — Hermes never
 * auto-tracks it (mock 08). Entries live client-side in localStorage per
 * user; only the aggregate score line is derived.
 */

export type StageStatus = "done" | "current" | "upcoming";

export type Stage = {
  id: string;
  name: string;
  status: StageStatus;
  /** Per-stage reflection notes ("what went well / what to improve"). */
  wentWell?: string;
  toImprove?: string;
};

export type Outcome = "in_progress" | "offer" | "rejected";

export type JournalEntry = {
  id: string;
  company: string;
  role: string;
  /** Stage count is user-defined; + Add stage appends. */
  stages: Stage[];
  /** Final-round sessions (e.g. system design / hiring manager / values). */
  sessions: string[];
  outcome: Outcome;
  /** For rejections: the stage the process ended at. */
  endedAtStage?: string;
  /** For offers: "what carried it". */
  reflection?: string;
  createdAt: number;
};

function key(uid: string): string {
  return `hermes:interviews:v1:${uid}`;
}

const EMPTY_ENTRIES: JournalEntry[] = [];

function parseEntries(raw: string | null): JournalEntry[] {
  if (!raw) return EMPTY_ENTRIES;
  try {
    return JSON.parse(raw) as JournalEntry[];
  } catch {
    return EMPTY_ENTRIES;
  }
}

/** Live view of the user's journal (empty until the uid is known). */
export function useJournal(uid: string | null): JournalEntry[] {
  return useStored("local", uid ? key(uid) : null, parseEntries, EMPTY_ENTRIES);
}

export function saveEntries(uid: string, entries: JournalEntry[]): void {
  writeStored("local", key(uid), JSON.stringify(entries));
}

export function newEntry(company: string, role: string): JournalEntry {
  const mk = (name: string, status: StageStatus): Stage => ({
    id: crypto.randomUUID(),
    name,
    status,
  });
  return {
    id: crypto.randomUUID(),
    company,
    role,
    stages: [
      mk("Recruiter", "current"),
      mk("Technical", "upcoming"),
      mk("Onsite", "upcoming"),
    ],
    sessions: [],
    outcome: "in_progress",
    createdAt: Date.now(),
  };
}

/**
 * Linear stage model: clicking a stage moves the "current" marker there —
 * everything before it is done, everything after upcoming. Clicking the
 * current stage completes it and advances.
 */
export function advanceStages(stages: Stage[], clickedId: string): Stage[] {
  const idx = stages.findIndex((s) => s.id === clickedId);
  if (idx < 0) return stages;
  const clicked = stages[idx];
  // Completing the current stage moves "current" to the next one (if any).
  const currentIdx =
    clicked.status === "current" ? Math.min(idx + 1, stages.length) : idx;
  return stages.map((s, i) => ({
    ...s,
    status:
      i < currentIdx ? "done" : i === currentIdx ? "current" : "upcoming",
  }));
}

export function currentStage(entry: JournalEntry): Stage | undefined {
  return entry.stages.find((s) => s.status === "current");
}

export function stagePosition(entry: JournalEntry): { n: number; of: number } {
  const idx = entry.stages.findIndex((s) => s.status === "current");
  return {
    n: idx >= 0 ? idx + 1 : entry.stages.length,
    of: entry.stages.length,
  };
}
