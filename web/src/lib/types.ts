// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

export type ScoreBreakdown = {
  role_fit: number;
  qualifications_match: number;
  seniority_match: number;
  comp_alignment: number;
  deal_breaker_penalty: number;
};

export type JobMatch = {
  overall_score: number;
  recommendation: string;
  reasoning: string;
  matched_strengths: string[];
  gaps: string[];
  red_flags_hit: string[];
  breakdown: ScoreBreakdown;
};

export type Job = {
  id: string;
  company: string;
  title: string;
  location: string | null;
  url: string;
  source: string;
  jd_raw: string;
  discovered_via: string;
  match: JobMatch;
};

export type CompanyEntry = {
  slug: string;
  added?: string | null;
  notes?: string | null;
  paused?: boolean;
};

export type BlockEntry = {
  platform: string;
  slug: string;
  blocked_at: string;
  reason: string;
};

export type CompaniesResponse = {
  known: Record<string, CompanyEntry[]>;
  unvetted: Record<string, CompanyEntry[]>;
  blocklist: BlockEntry[];
};

export type Decision = "approved" | "rejected" | "starred";
export type CompanyActionType = "promote" | "block" | "dismiss" | "pause";

export type RoleBullets = {
  company: string;
  role: string;
  bullets: string[];
};

export type StatusEvent = {
  at: string;
  status: string;
  note?: string | null;
};

export type ApplicationStatus =
  | "queued"
  | "tailoring"
  | "ready_for_review"
  | "submitting"
  | "submitted"
  | "failed"
  | "responded";

export type Confirmation = {
  submitted_at: string;
  confirmation_id?: string | null;
  screenshot_uri?: string | null;
};

export type Screenshot = { name: string; uri: string };

export type Application = {
  id: string;
  user_id: string;
  job_id: string;
  job_company?: string | null;
  job_title?: string | null;
  job_url?: string | null;
  status: ApplicationStatus;
  resume_variant_uri?: string | null;
  objective_text?: string | null;
  cover_letter_uri?: string | null;
  master_bullets: RoleBullets[];
  tailored_bullets: RoleBullets[];
  last_submitted_at?: string | null;
  screenshots?: Screenshot[];
  confirmation?: Confirmation | null;
  timeline: StatusEvent[];
};
