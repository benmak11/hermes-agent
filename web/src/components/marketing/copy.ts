// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/**
 * Every human-readable string on the marketing site (facelift PR 3). No
 * component under `marketing/` or `app/(marketing)/` holds prose of its own —
 * `copy.test.ts` and the PR's grep gate enforce it — so the copy can be
 * sharpened here without touching layout.
 */

const lede =
  "Hermes finds the roles and writes the applications from your real history. You approve them with a tap and every interview that follows shows up on one map.";

export const copy = {
  meta: { title: "Hermes — the job search, run for you", description: lede },
  nav: {
    brand: "Hermes",
    ariaLabel: "Primary",
    features: "Features",
    items: [
      { title: "Journeys", blurb: "Where you stand in every interview" },
      { title: "Applying for you", blurb: "Roles matched and written up from your history" },
      { title: "Insights", blurb: "What your own notes say about where you stall" },
    ],
    howItWorks: "How it works",
    security: "Security & privacy",
    signIn: "Sign in",
    cta: "Request an invite",
    openApp: "Open Hermes →",
  },
  hero: {
    badgeNew: "New",
    badge: "Journeys are ways to see where you stand in every interview",
    h1Line1: "Hermes does the applying.",
    h1Em: "You",
    h1Line2Rest: " do the interviews.",
    lede,
    cta: "Request an invite",
    secondary: "See how it works",
    subline: "Invite-only while we're small. Free while you're searching.",
    demo: {
      frameTitle: "Your journeys",
      live: "3 live",
      role: "Staff Engineer, Payments",
      company: "· Shopify",
      pill: "Hermes applied for you",
      stages: {
        applied: { name: "Applied", note: "2 Sep" },
        recruiter: { name: "Recruiter call", note: "went well" },
        design: { name: "System design", note: "Fri · you're here" },
        manager: { name: "Hiring manager", note: "not booked" },
        decision: { name: "Decision", note: "—" },
      },
    },
  },
  proof: [
    { before: "Every application is written from ", strong: "your real history", after: ", not a template." },
    { before: "", strong: "Nothing is sent until you approve it.", after: " No exceptions." },
    { before: "Roles ", strong: "you found yourself", after: " track the same way." },
  ],
  how: {
    eyebrow: "How it works",
    h2: "The searching and the paperwork are ours. The conversations are yours.",
    beats: [
      {
        pill: "01 — It applies",
        h3: "It finds the roles and writes them up",
        p: "Hermes reads your history once, then matches roles and drafts each application from what's actually in it. You read the draft, approve it, and it goes. Roles you found yourself sit in the same list.",
      },
      {
        pill: "02 — You interview",
        h3: "Know exactly where you stand",
        p: "After the recruiter call, tell Hermes what they described, and it drafts the stages for you to check. From then on each process reads as one line: what's done, where you are, what's next.",
      },
      {
        pill: "03 — You learn",
        h3: "Remember what happened, and why",
        p: "Two minutes after each interview, note how it went. When a process ends — offer or not — you see the whole arc in one place, and what you wrote becomes the prep list for the next one.",
      },
    ],
    mock1: {
      rows: [
        { title: "Senior Engineer, Billing", sub: "Anthropic · remote", chip: "strong match" },
        { title: "Staff Engineer, Payments", sub: "Shopify · hybrid", chip: "strong match" },
      ],
      apply: "Apply for me",
      skip: "Not this one",
    },
    mock2: {
      label: "Draft — check before saving",
      rows: [
        { name: "System design", chip: "60 min" },
        { name: "Hiring manager", chip: "45 min" },
        { name: "Values conversation", chip: "tbc" },
      ],
    },
    mock3: {
      label: "Where your journeys end",
      rows: [
        { name: "Recruiter call", count: "5/5" },
        { name: "Technical", count: "2/5" },
        { name: "System design", count: "1/3" },
      ],
      note: "Conversations are your strength. The timed rooms are where things stop, so that's what goes in your prep list.",
    },
  },
  features: {
    eyebrow: "Features",
    h2: "After you hit send, this is where it all lives.",
    items: [
      {
        icon: "◷",
        h3: "One line per company",
        p: "What's done, where you are, what's still ahead — legible at a glance, however many you're juggling.",
      },
      {
        icon: "✎",
        h3: "Their email becomes your plan",
        p: "Tell Hermes what the recruiter described. It drafts the stages, and nothing is saved until you say it's right.",
      },
      {
        icon: "☑",
        h3: "Prep that remembers",
        p: "Your checklist starts from what tripped you up last time, not from generic advice.",
      },
      {
        icon: "◐",
        h3: "Two minutes, while it's fresh",
        p: "One rating, a few taps, one sentence. Skip it if you want — only you ever read it.",
      },
      {
        icon: "↺",
        h3: "Look back either way",
        p: "Offers and rejections close the same way: the whole arc, then what you'd keep from it.",
      },
      {
        icon: "⌁",
        h3: "Add your own",
        p: "Found something yourself? Put it in and it tracks exactly like the rest.",
      },
    ],
  },
  security: {
    eyebrow: "Security & privacy",
    h2: "Your search is nobody else's business.",
    p: "Not your employer's, not a recruiter's, and not ours to sell. Interview notes are private to you by default, and stay that way.",
    points: [
      { strong: "Nothing sends without you.", rest: " Every application waits for your approval." },
      { strong: "Your notes stay yours.", rest: " What you write after an interview is never training data." },
      { strong: "Leave with your data.", rest: " Delete everything, any time." },
    ],
  },
  closing: {
    h2: "The search is long. Don't run it from memory.",
    p: "Setup takes about four minutes, and it's free while you're searching.",
    cta: "Request an invite",
  },
  footer: {
    tagline: "The job search, run for you — and a map of every interview you're in.",
    product: { heading: "Product", howItWorks: "How it works", journeys: "Journeys", insights: "Insights" },
    company: { heading: "Company", security: "Security & privacy", contact: "Contact", terms: "Terms" },
    start: { heading: "Get started", cta: "Request an invite", signIn: "Sign in" },
    copyright: "© Hermes 2026",
  },
  pages: {
    security: {
      title: "Security & privacy",
      h1: "Security & privacy",
      p: "Hermes keeps your résumé and interview notes only to run your search. Nothing is sent to an employer until you approve it, what you write after an interview is never used to train anything, and you can delete your account and everything in it from your profile at any time.",
    },
    contact: {
      title: "Contact",
      h1: "Contact",
      p: "Hermes is small and invite-only. If you were invited, the person who invited you is the fastest way to reach us; a public address will be listed here soon.",
    },
    terms: {
      title: "Terms",
      h1: "Terms",
      p: "Hermes is an invite-only preview. There is nothing to pay for yet, and the full terms will be published before there is. Until then the short version: your data is yours, and you can delete it whenever you like.",
    },
  },
} as const;
