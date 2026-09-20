// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/**
 * Pure helpers for the warm journey track. Same linear stage model as
 * `advanceStages` in `@/lib/interviews` (before the marker = done, at it =
 * current, after = upcoming), kept separate so the track can render a
 * journey from any list of names without touching the journal's storage.
 *
 * Not named `journeyTrack.ts`: on a case-insensitive filesystem (macOS) that
 * would shadow `JourneyTrack.tsx` for extensionless imports.
 */

export type TrackStatus = "done" | "current" | "upcoming";

export type TrackStage = {
  id: string;
  name: string;
  status: TrackStatus;
  /** Small line under the name ("2 Sep", "went well", "not booked"). */
  note?: string;
  noteTone?: "good" | "muted" | "accent";
};

/** Names → stages with exactly one `current` at `currentIdx`; ids are the index. */
export function deriveTrack(names: string[], currentIdx: number): TrackStage[] {
  return names.map((name, i) => ({
    id: String(i),
    name,
    status: i < currentIdx ? "done" : i === currentIdx ? "current" : "upcoming",
  }));
}
