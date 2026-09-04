// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { readStored, useStored, writeStored } from "@/lib/localStore";
import type { Decision } from "@/lib/types";

/**
 * Review-session bookkeeping (mock 07): decision counts, the pace estimate,
 * and the shared min-score setting. Persisted in localStorage per user so
 * progress and counts survive across sessions, as the design specifies.
 */
export type SessionStats = {
  approved: number;
  skipped: number;
  starred: number;
  /** Epoch ms of recent decisions — the rolling window for the pace estimate. */
  times: number[];
  /**
   * The server's `data_epoch` these counts were accumulated against, or null
   * for counts stored before this field existed. When it stops matching the
   * profile's epoch the counts describe jobs that no longer exist, and are
   * dropped — see `reconcileStats`.
   */
  epoch?: string | null;
};

export const EMPTY_STATS: SessionStats = {
  approved: 0,
  skipped: 0,
  starred: 0,
  times: [],
  epoch: null,
};

const PACE_WINDOW = 8;

function statsKey(uid: string): string {
  return `hermes:session:v1:${uid}`;
}

function parseStats(raw: string | null): SessionStats {
  if (!raw) return EMPTY_STATS;
  try {
    return { ...EMPTY_STATS, ...JSON.parse(raw) };
  } catch {
    return EMPTY_STATS;
  }
}

export function loadStats(uid: string): SessionStats {
  return parseStats(readStored("local", statsKey(uid)));
}

export function saveStats(uid: string, stats: SessionStats): void {
  writeStored("local", statsKey(uid), JSON.stringify(stats));
}

/**
 * Drop stored counts that describe a server-side dataset which no longer
 * exists, and re-stamp them with the current epoch.
 *
 * A wipe (`cli/reset_user.py`, or `POST /account/delete` followed by signing
 * up again) clears Firestore but cannot reach `localStorage`, so the review
 * header would go on reporting "29 of 29 reviewed" for an account whose jobs
 * are gone — `reviewed` comes from here while `remaining` comes from the
 * server, and `total` is their sum.
 *
 * Only acts on a definite disagreement. A null `epoch` argument means the
 * profile hasn't loaded or the stamp write failed, which is not evidence of
 * anything and must not throw away a real session's counts.
 */
export function reconcileStats(uid: string, epoch: string | null): void {
  if (!epoch) return;
  const stored = loadStats(uid);
  if (stored.epoch === epoch) return;
  saveStats(uid, { ...EMPTY_STATS, epoch });
}

/** Live view of the user's session stats. */
export function useSessionStats(uid: string | null): SessionStats {
  return useStored(
    "local",
    uid ? statsKey(uid) : null,
    parseStats,
    EMPTY_STATS,
  );
}

const FIELD: Record<Decision, keyof SessionStats> = {
  approved: "approved",
  rejected: "skipped",
  starred: "starred",
};

export function recordDecision(
  stats: SessionStats,
  decision: Decision,
): SessionStats {
  return {
    ...stats,
    [FIELD[decision]]: (stats[FIELD[decision]] as number) + 1,
    times: [...stats.times, Date.now()].slice(-PACE_WINDOW),
  };
}

export function revertDecision(
  stats: SessionStats,
  decision: Decision,
): SessionStats {
  return {
    ...stats,
    [FIELD[decision]]: Math.max(0, (stats[FIELD[decision]] as number) - 1),
    times: stats.times.slice(0, -1),
  };
}

export function reviewedCount(stats: SessionStats): number {
  return stats.approved + stats.skipped + stats.starred;
}

/** "~N min at your pace" from the rolling average gap between decisions. */
export function paceMinutes(
  stats: SessionStats,
  remaining: number,
): number | null {
  if (remaining <= 0 || stats.times.length < 3) return null;
  const t = stats.times;
  const avgGap = (t[t.length - 1] - t[0]) / (t.length - 1);
  if (avgGap <= 0 || avgGap > 5 * 60_000) return null;
  return Math.max(1, Math.round((remaining * avgGap) / 60_000));
}

// ---- Min-score preference (shared by the review page + profile page) ----

const MIN_SCORE_KEY = "hermes:minScore";

function parseMinScore(raw: string | null): number {
  const v = Number(raw);
  return Number.isFinite(v) && raw !== null && v >= 0 ? v : 60;
}

export function useMinScore(): number {
  return useStored("local", MIN_SCORE_KEY, parseMinScore, 60);
}

export function saveMinScore(v: number): void {
  writeStored("local", MIN_SCORE_KEY, String(v));
}

// ---- First-run flag (set after onboarding; drives the mock-06 states) ----

const FIRST_RUN_KEY = "hermes:firstRun";
const FIRST_RUN_TTL = 5 * 60_000;

export function markFirstRun(): void {
  writeStored("session", FIRST_RUN_KEY, String(Date.now()));
}

export function clearFirstRun(): void {
  writeStored("session", FIRST_RUN_KEY, null);
}

function parseFirstRun(raw: string | null): boolean {
  const at = Number(raw);
  return raw !== null && Number.isFinite(at) && Date.now() - at < FIRST_RUN_TTL;
}

export function useFirstRun(): boolean {
  return useStored("session", FIRST_RUN_KEY, parseFirstRun, false);
}
