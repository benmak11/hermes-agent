// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { signOut } from "firebase/auth";
import Link from "next/link";

import { UserAvatar } from "@/components/UserAvatar";
import { SANS } from "@/components/warm/styles";
import { auth } from "@/lib/firebase";
import { APP_HOME } from "@/lib/nav";

type Section =
  | "review"
  | "companies"
  | "applications"
  | "profile"
  | "journeys"
  | "tracking";

const LINKS: { section: Section; href: string; label: string }[] = [
  { section: "review", href: APP_HOME, label: "Review" },
  { section: "tracking", href: "/app/tracking", label: "Applications" },
  { section: "journeys", href: "/app/journeys", label: "Journeys" },
  { section: "companies", href: "/app/settings/companies", label: "Companies" },
];

/** The quiet label after the wordmark (design 16: "Hermes  Journeys"). */
const SECTION_LABEL: Record<Section, string> = {
  review: "Review",
  tracking: "Applications",
  journeys: "Journeys",
  companies: "Companies",
  profile: "Profile",
  applications: "Your application",
};

/**
 * 60px warm app header (design 16): terracotta logo tile + wordmark + section
 * label left; Review · Tracking · Journeys · Companies · Sign out + avatar
 * right, the current page's link active-styled (terracotta underline). The
 * avatar opens /app/profile and wears a terracotta ring while there. `center`
 * (session progress) and `pill` (discovery status) are slots for the review
 * screen; the center slot hides under 900px. Companies keeps its "← Back to
 * jobs" shortcut. Jakarta is set here so the bar reads warm on every /app page.
 */
export function TopNav({
  section,
  center,
  pill,
}: {
  section: Section;
  center?: React.ReactNode;
  pill?: React.ReactNode;
}) {
  return (
    <header
      className="sticky top-0 z-10 flex h-[60px] items-center justify-between border-b px-6"
      style={{ fontFamily: SANS, background: "var(--surface-warm)", borderColor: "#f0e3d3" }}
    >
      <Link href="/app" className="flex items-center gap-2.5">
        <span
          className="flex h-7 w-7 items-center justify-center rounded-[9px] text-sm font-bold"
          style={{ background: "var(--terracotta)", color: "#fff9f2" }}
        >
          H
        </span>
        <span className="text-[15px] font-bold" style={{ color: "var(--ink)" }}>
          Hermes
        </span>
        <span className="text-[12.5px]" style={{ color: "#b0a08d" }}>
          {SECTION_LABEL[section]}
        </span>
      </Link>

      {center && (
        <div className="wm-nav-center absolute left-1/2 -translate-x-1/2">{center}</div>
      )}

      {section === "companies" ? (
        <Link href="/app" className="wm-link text-[13px] font-semibold">
          ← Back to jobs
        </Link>
      ) : (
        <div className="flex items-center gap-[18px]">
          {pill}
          {LINKS.map((l) => (
            <Link
              key={l.section}
              href={l.href}
              className={
                l.section === section
                  ? "wm-nav-active text-[13px]"
                  : "wm-muted-link text-[13px] font-medium"
              }
            >
              {l.label}
            </Link>
          ))}
          <button
            onClick={() => signOut(auth)}
            className="wm-nav-quiet text-[13px]"
          >
            Sign out
          </button>
          <Link
            href="/app/profile"
            aria-label="Profile"
            className="rounded-full"
            style={
              section === "profile"
                ? {
                    boxShadow:
                      "0 0 0 2px var(--surface-warm), 0 0 0 4px var(--terracotta)",
                  }
                : undefined
            }
          >
            <UserAvatar />
          </Link>
        </div>
      )}
    </header>
  );
}
