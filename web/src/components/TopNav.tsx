// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { signOut } from "firebase/auth";
import Link from "next/link";

import { UserAvatar } from "@/components/UserAvatar";
import { auth } from "@/lib/firebase";

type Section =
  | "review"
  | "companies"
  | "applications"
  | "profile"
  | "interviews"
  | "tracking";

const LINKS: { section: Section; href: string; label: string }[] = [
  { section: "review", href: "/", label: "Review" },
  { section: "tracking", href: "/tracking", label: "Tracking" },
  { section: "interviews", href: "/interviews", label: "Interviews" },
  { section: "companies", href: "/settings/companies", label: "Companies" },
];

/**
 * 56px translucent app nav (mock spec): logo + `/ route` breadcrumb left;
 * Review · Interviews · Companies · Sign out + avatar right, with the current
 * page's link omitted. The avatar opens /profile and wears a blue focus ring
 * while there. `center` (session progress) and `pill` (discovery status) are
 * slots for the review screen. Companies keeps its "← Back to jobs" shortcut.
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
      className="sticky top-0 z-10 flex h-14 items-center justify-between border-b px-6 backdrop-blur"
      style={{ background: "var(--nav-bg)", borderColor: "var(--border)" }}
    >
      <Link href="/" className="flex items-center gap-2.5">
        <span
          className="flex h-[26px] w-[26px] items-center justify-center rounded-[7px] text-sm font-bold"
          style={{ background: "var(--text)", color: "var(--surface)" }}
        >
          H
        </span>
        <span className="text-[15px] font-semibold" style={{ color: "var(--text)" }}>
          Hermes
        </span>
        <span
          className="ml-1.5 font-mono text-xs font-medium"
          style={{ color: "var(--subtle)" }}
        >
          / {section}
        </span>
      </Link>

      {center && (
        <div className="absolute left-1/2 -translate-x-1/2">{center}</div>
      )}

      {section === "companies" ? (
        <Link
          href="/"
          className="text-[13px] font-medium"
          style={{ color: "var(--accent)" }}
        >
          ← Back to jobs
        </Link>
      ) : (
        <div className="flex items-center gap-[18px]">
          {pill}
          {LINKS.filter((l) => l.section !== section).map((l) => (
            <Link
              key={l.section}
              href={l.href}
              className="text-[13px] font-medium"
              style={{ color: "var(--label)" }}
            >
              {l.label}
            </Link>
          ))}
          <button
            onClick={() => signOut(auth)}
            className="text-[13px]"
            style={{ color: "var(--subtle)" }}
          >
            Sign out
          </button>
          <Link
            href="/profile"
            aria-label="Profile"
            className="rounded-full"
            style={
              section === "profile"
                ? { boxShadow: "0 0 0 2px var(--bg), 0 0 0 4px var(--accent)" }
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
