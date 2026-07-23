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

// ---- Profile (mirrors models/profile.py MasterProfile) ----

export type ProfileBullet = {
  text: string;
  tags: string[];
  impact?: string | null;
};

export type ProfileExperience = {
  company: string;
  role: string;
  start: string; // ISO date
  end?: string | null; // null = current
  location?: string | null;
  bullets: ProfileBullet[];
};

export type ProfileEducation = {
  institution: string;
  degree: string;
  field: string;
  start_year: number;
  end_year?: number | null;
};

export type Residence = {
  country: string;
  state?: string | null;
  city?: string | null;
};

export type RemoteStyle = "remote" | "hybrid" | "onsite";

export type JobPreferences = {
  target_role_families: string[];
  target_titles: string[];
  target_seniorities: string[];
  min_comp_total?: number | null;
  remote_policy: RemoteStyle[];
  locations: string[];
  must_haves: string[];
  deal_breakers: string[];
};

export type Profile = {
  user_id: string;
  full_name: string;
  email: string;
  phone?: string | null;
  location: string;
  residence?: Residence | null;
  links: Record<string, string>;
  objective_template: string;
  experience: ProfileExperience[];
  education: ProfileEducation[];
  skills: Record<string, string[]>;
  preferences: JobPreferences;
};

export type ProfileResponse = {
  profile: Profile | null;
  onboarding_complete: boolean;
};

// ---- Auto-discovery settings (mirrors models/settings.py) ----

export type DiscoverySettings = {
  auto_discovery: boolean;
  discovery_interval_hours: number;
  liveness_sweep: boolean;
  sweep_interval_hours: number;
};

export type DiscoveryState = {
  last_discovery_at?: string | null;
  last_sweep_at?: string | null;
  last_discovery?: {
    new_jobs: number;
    scored: number;
    failed: number;
    jobs_fetched?: number;
    jobs_by_platform?: Record<string, number>;
    boards_failed?: number;
    empty_boards?: number;
    duration_ms?: number;
    run_id?: string;
    trigger?: string;
  } | null;
  last_sweep?: {
    checked: number;
    removed: number;
    boards_failed: number;
    duration_ms?: number;
    run_id?: string;
    trigger?: string;
  } | null;
};

export type DiscoverySettingsResponse = {
  settings: DiscoverySettings;
  state: DiscoveryState;
  next_discovery_at?: string | null;
  next_sweep_at?: string | null;
};

export type Decision = "approved" | "rejected" | "starred";
/** What POST /jobs/{id}/decide accepts — "pending" reverts a decision. */
export type DecideValue = Decision | "pending";
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
  | "responded"
  | "needs_input"
  | "posting_removed";

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
  /** With status=needs_input: the required questions left for the user. */
  unanswered_questions?: string[];
  screenshots?: Screenshot[];
  confirmation?: Confirmation | null;
  timeline: StatusEvent[];
};
