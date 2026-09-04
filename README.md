# Hermes

Hermes is an agentic **job-search assistant**: it builds a profile from your
résumé, discovers and ranks openings against it, tailors a résumé per posting,
and submits applications — surfaced through a web app and backed by a set of
LLM-backed pipelines on Google Cloud.

Those pipelines run Vertex AI Gemini where judgment is needed and plain Python
everywhere else: Flash to parse a job description, Pro to score it against the
profile behind a deterministic geo-eligibility gate and per-user budget caps,
Flash again to tailor the résumé. They are driven by CLI runners and a Cloud
Tasks worker, fronted by a FastAPI gateway and a Next.js app, with Firestore
for state — all deployed on Cloud Run.

## How it works

The end-to-end funnel:

```
onboarding → discovery → matching / vetting → tailoring → application → tracking
 (résumé →     (scout      (rank vs. profile,   (rerank      (submit to     (status +
  profile)      sources)    geo-eligibility)     bullets +    the ATS)       interview
                                                 objective +                  journal)
                                                 ATS docx)
```

That funnel *is* the architecture: each stage is a deterministic pipeline in
`tools/` with a `cli/` runner for batch work and an API or worker entry point
for the app, calling a model only at the steps that need one.

### Screens

The web app (`web/`, Next.js) is where a user rides that funnel:

| Login | Onboarding — résumé parse review |
|---|---|
| ![Login: Google or email sign-in, gated to invited reviewers](docs/screenshots/login.png) | ![Onboarding: confirm and correct what Hermes parsed from the résumé](docs/screenshots/onboarding-profile-review.png) |

| Discovery — companies | Matching / vetting — job review |
|---|---|
| ![Discovery: companies scouted in the background from the résumé](docs/screenshots/discovery-companies.png) | ![Job review: approve, skip, or star ranked postings with a score and recommendation](docs/screenshots/matching-job-review.png) |

| Matching — score breakdown | Tracking — interview journal |
|---|---|
| ![Score breakdown, empty, and loading states for the job review card](docs/screenshots/matching-job-review-detail.png) | ![Interview journal: user-logged stages, outcomes, and reflections per application](docs/screenshots/tracking-interviews.png) |

| Profile |
|---|
| ![Profile: résumé versions, match preferences, skills, and experience](docs/screenshots/profile.png) |

`/` is job review (approve/skip/star, keyboard-driven, a running score/recommendation
per posting); `/tracking` is the live application pipeline, filled in as the
submitter works; `/interviews` is a separate, user-owned journal —
Hermes logs the score, the user logs stages, outcomes, and what to improve;
`/settings/companies` is the discovery source list (rescan/block per company);
`/profile` holds résumé versions, match preferences, skills, and experience.
Tailored résumés are reviewed and downloaded per application at
`/applications/{id}/review`.

(Screens above are drawn from current design mocks, not live-app screenshots.
Paywall/monetization UI is in progress and intentionally left out of this
overview.)

| Stage | Model | What it does |
|-------|-------|--------------|
| Discovery (`tools/discovery/`) | — (no model) | Fans out across job boards and company career pages concurrently |
| Matching (`tools/matching/pipeline.py`) | `gemini-flash-latest` → `gemini-3.1-pro-preview` | Flash parses the JD; a free Python pre-filter and geo-eligibility gate drop what Pro shouldn't be paid to read; Pro scores the rest against the profile |
| Tailoring (`tools/tailoring/`) | `gemini-flash-latest` | Reranks bullets, writes an objective, renders an ATS-safe résumé |
| Application (`tools/submitters/`) | — (Playwright) | Submits to Greenhouse by driving the real form; falls back to manual apply |
| Tracking | — | Records application status (deferred) |

## Components

| Path | What it is |
|------|-----------|
| `tools/` | The pipelines themselves — `discovery/`, `matching/`, `tailoring/`, `submitters/`, `applications/`, `profile/`, `account/`, `ats/`. |
| `cli/` | Batch runners for the pipelines (import résumé, sync profile, discovery, matching, tailoring, user migration). |
| `api/` | FastAPI gateway. Serves the Firestore-backed web API (`routes/{jobs,companies,applications,profile}.py`, Firebase-auth deps) and the `/tasks/*` worker handlers. |
| `web/` | Next.js 16 frontend — login, onboarding/profile, job vetting (review/approve/skip/star), applications, company vetting. |
| `models/` | Pydantic schemas (`profile`, `job`, `match`, `application`). |
| `deployment/` | Terraform for Cloud Run + supporting infrastructure. |
| `tests/` | Unit and integration tests. |

## Data & storage

- **Firestore (Native)** — the profile lives at `users/{uid}`; `jobs`,
  `applications`, and company data are subcollections.
- **Cloud Storage** — tailored résumés (`.docx`) and submission screenshots
  (the `RESUME_BUCKET`).
- **Firebase Auth** — the web app signs users in; the API verifies Firebase ID
  tokens (with a local dev bypass gated on `AUTH_DEV_MODE`).

## Current state

- **Live:** Firebase-auth login, onboarding (résumé → profile, with an
  editable parse-review step), discovery (background company/job scouting
  with rescan/block controls), matching with a location-eligibility gate, the
  job-vetting web UI (`/`, approve/skip/star with a score breakdown),
  tailoring (bullets + objective + ATS-safe résumé to GCS with a diff/review
  screen at `/applications/{id}/review`), the Greenhouse application path
  (Playwright submit with SSE progress, screenshots, idempotency, and a
  manual-apply fallback), the `/tracking` application-status pipeline (filled
  in as the submitter writes status), and the `/interviews` journal (user-logged
  interview stages/outcomes/reflections — Hermes contributes only the match
  score, never auto-tracks).
- **Deferred:** the "Computer Use" browser path for submitting to non-Greenhouse
  ATSes, and automatic Gmail-based response detection for tracking.
- **In progress (not covered here):** monetization/paywall.

## Deployment

Two Cloud Run services in `us-central1` (scale-to-zero): **`hermes-api`** (the
FastAPI gateway) and **`hermes-web`** (the Next.js app). CI/CD runs through
GitHub Actions — checks on every PR, and a deploy to Cloud Run on merge to
`main` via keyless Workload Identity Federation.

Built-in telemetry exports to Cloud Trace, BigQuery, and Cloud Logging.

## Running it

Local setup, the data-pipeline runbook, and deploy steps are kept out of this
README. See **`docs/RUNNING.md`** (a local operator runbook, not tracked in
git).
