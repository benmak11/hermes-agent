# Hermes web

The Next.js 16 (App Router) frontend for Hermes — the surface a user rides the
job-search funnel through: sign in, review the parsed résumé, watch discovery
and matching fill in, vet jobs, track applications, and log interviews. See
the root [`README.md`](../README.md) for the end-to-end system and
screenshots of these screens.

## Routes

| Route | Screen |
|---|---|
| `/` | Marketing site (static; hero is demo data through the shared `JourneyTrack`) |
| `/security`, `/contact`, `/terms` | Placeholder pages (heading + paragraph) under the marketing nav and footer, until they are designed |
| `/login`, `/signup` | Google or email sign-in / create account (Firebase Auth); `/login?next=/app/...` returns you there after sign-in |
| `/onboarding`, `/onboarding/review` | Upload a résumé, then confirm/correct what Hermes parsed before matching starts |
| `/app` | Job review — approve/skip/star ranked postings, keyboard-driven, with a score + recommendation breakdown |
| `/app/tracking` | Application pipeline (pipeline/starred/skipped tabs), filled in as the submitter writes status |
| `/app/applications/{id}/review` | Tailored résumé diff/review + `.docx` download for a single application |
| `/app/interviews` | User-owned interview journal — Hermes contributes only the match score; stages, outcomes, and reflections are logged by the user |
| `/app/settings/companies` | Discovery source list — rescan or block companies |
| `/app/profile` | Résumé versions, match preferences, skills, and experience |

Old top-level app paths (`/tracking`, `/profile`, …) 307 to `/app/...` via
`redirects()` in `next.config.ts`; `/app/*` is gated client-side by
`src/app/app/layout.tsx`.

The marketing routes live in the `src/app/(marketing)/` route group, whose
`layout.tsx` is the shared chrome (sand wrapper, `MarketingNav`, `Footer`).
Sections are in `src/components/marketing/`: every string is in `copy.ts`
and every href in `links.ts` (hashes are `/#x` so the nav works from the
placeholder pages; the four "Request an invite" CTAs carry `?from=` sources
that `/signup` stashes). `MarketingNav` is the only client component — it
owns the Features dropdown and swaps "Sign in / Request an invite" for
"Open Hermes →" once Firebase resolves a user. Styles are inline; what inline
styles cannot express (hover, the reduced-motion transition, the < 720px
`mk-desktop-only` breakpoint, the `html:has(.mk)` light-only ground) is the
`.mk-*` block at the end of `globals.css`.

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

## Warm design system (facelift, in progress)

`globals.css` carries a second, light-only token set next to the grey/blue
ramp: `--sand`, `--cream`, `--ink`…`--ink-4`, `--terracotta*`, `--sage*`,
`--honey*`, `--brick`, plus `--surface-warm` / `--border-warm` /
`--border-warm-hair`. The last three are suffixed because `--surface` and
`--border` already name the grey ones. `@keyframes jpulse` (the journey
track's halo) is distinct from `hpulse`, the opacity blink behind `.h-pulse`
on the review page. `--font-jakarta` and `--font-instrument` are loaded in
`layout.tsx` as variables (preloaded, consumed by the marketing site);
`--font-sans` is still Geist.

`src/components/warm/` holds the shared primitives (`JourneyTrack`, `Pill`,
`CompanyTile`, `MatchChip`) and the pure helper `journeyStages.ts` — not
`journeyTrack.ts`, which would shadow `JourneyTrack.tsx` on a case-insensitive
filesystem. They use warm tokens and inline styles only and must not import
`@/lib/firebase`, `@/lib/api`, `next/navigation`, or any CSS.

```bash
npm test       # vitest, node environment — no jsdom, no browser
```

Tests stay pure: import only `src/lib/*`, `src/components/warm/*` and
`src/components/marketing/*`, and render components through
`react-dom/server`'s `renderToStaticMarkup`. Components that only read auth
import `useAuth` from `src/lib/authContext.ts` (no Firebase import) so tests
can wrap them in `AuthContext.Provider`; `src/lib/auth.tsx` re-exports it.
