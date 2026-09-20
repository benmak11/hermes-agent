// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { TrackStage } from "@/components/warm/journeyStages";
import { copy } from "./copy";

/**
 * Static demo data for the hero's journey track — the same `JourneyTrack`
 * the app board will render from real data, so the site cannot drift from
 * the product. Words come from `copy.ts`; the shape (which stage is current,
 * which note tone) lives here.
 */

const s = copy.hero.demo.stages;

export const HERO_STAGES: readonly TrackStage[] = [
  { id: "applied", name: s.applied.name, status: "done", note: s.applied.note, noteTone: "muted" },
  { id: "recruiter", name: s.recruiter.name, status: "done", note: s.recruiter.note, noteTone: "good" },
  { id: "design", name: s.design.name, status: "current", note: s.design.note, noteTone: "accent" },
  { id: "manager", name: s.manager.name, status: "upcoming", note: s.manager.note, noteTone: "muted" },
  { id: "decision", name: s.decision.name, status: "upcoming", note: s.decision.note, noteTone: "muted" },
];

export const HERO_ROW = { initial: "S", hue: "violet" } as const;
