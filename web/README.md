# Hermes web

The Next.js 16 (App Router) frontend for Hermes — the surface a user rides the
job-search funnel through: sign in, review the parsed résumé, watch discovery
and matching fill in, vet jobs, track applications, and log interviews. See
the root [`README.md`](../README.md) for the end-to-end system and
screenshots of these screens.

## Routes

| Route | Screen |
|---|---|
| `/login` | Google or email sign-in (Firebase Auth) |
| `/onboarding`, `/onboarding/review` | Upload a résumé, then confirm/correct what Hermes parsed before matching starts |
| `/` | Job review — approve/skip/star ranked postings, keyboard-driven, with a score + recommendation breakdown |
| `/tracking` | Application pipeline (pipeline/starred/skipped tabs), filled in as the Application agent writes status |
| `/applications/{id}/review` | Tailored résumé diff/review + `.docx` download for a single application |
| `/interviews` | User-owned interview journal — Hermes contributes only the match score; stages, outcomes, and reflections are logged by the user |
| `/settings/companies` | Discovery source list — rescan or block companies |
| `/profile` | Résumé versions, match preferences, skills, and experience |

## Stack

- **Next.js 16** (App Router) + **React 19**, styled with **Tailwind CSS 4**
- **Firebase Auth** for sign-in; the client attaches a Firebase ID token to
  API calls
- **TanStack Query** for data fetching/caching against the FastAPI gateway
  (`api/routes/{jobs,companies,applications,profile}.py`)
- `NEXT_PUBLIC_API_BASE` points the client at the gateway (defaults to
  `http://localhost:8080` for local dev)

## Getting started

```bash
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The gateway
(`uv run uvicorn api.main:app --reload`, from the repo root) needs to be
running for anything past `/login` to load data.

```bash
npm run lint   # eslint
npm run build  # production build
```
