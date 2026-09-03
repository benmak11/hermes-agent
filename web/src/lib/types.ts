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
  /** Paused in the global pool (a git edit) — skipped for everyone. */
  paused?: boolean;
  /** Excluded by *this* user's overlay. Not the same thing as `paused`. */
  excluded?: boolean;
};

/** An entry on the global blocklist: git-shipped, and applies to every user. */
export type BlockEntry = {
  platform: string;
  slug: string;
  blocked_at: string;
  reason: string;
};

/** One company the signed-in user has excluded from their own fetch set. */
export type UserExclusion = {
  platform: string;
  slug: string;
};

export type CompaniesResponse = {
  /** The global pool, annotated with this user's `excluded` flag. */
  known: Record<string, CompanyEntry[]>;
  unvetted: Record<string, CompanyEntry[]>;
  /** Global — everyone's blocklist, not this user's doing and not theirs to undo. */
  blocklist: BlockEntry[];
  /** This user's exclusions, listed separately: one can outlive its pool entry. */
  excluded: UserExclusion[];
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
    cost_usd?: number | null;
    jobs_fetched?: number;
    jobs_by_platform?: Record<string, number>;
    boards_failed?: number;
    empty_boards?: number;
    duration_ms?: number;
    run_id?: string;
    trigger?: string;
    /** Scoring budget: what this cycle was granted, and what is left after it.
     *  Absent on runs taken before the cap existed, or run with --ignore-budget. */
    budget_granted?: number;
    budget_remaining_cycle?: number;
    budget_remaining_day?: number;
    budget_capped?: boolean;
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
/**
 * What POST /companies/action accepts. All three exclude the company from this
 * user's fetch set and differ only in the reason recorded. `promote` is not
 * here: it was a global operator action that never changed the fetch set, and
 * there is no global write path left — promotion is a git edit to known.yaml.
 */
export type CompanyActionType = "block" | "dismiss" | "pause";

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
  screenshots?: Screenshot[];
  confirmation?: Confirmation | null;
  timeline: StatusEvent[];
};
