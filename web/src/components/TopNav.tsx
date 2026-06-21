// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { signOut } from "firebase/auth";
import Link from "next/link";

import { auth } from "@/lib/firebase";

export function TopNav({
  section,
}: {
  section: "review" | "companies" | "applications";
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
          className="ml-1.5 font-mono text-xs"
          style={{ color: "var(--subtle)" }}
        >
          / {section}
        </span>
      </Link>

      {section === "review" ? (
        <div className="flex items-center gap-[18px]">
          <Link
            href="/applications"
            className="text-[13px] font-medium"
            style={{ color: "var(--label)" }}
          >
            Applications
          </Link>
          <Link
            href="/settings/companies"
            className="text-[13px] font-medium"
            style={{ color: "var(--label)" }}
          >
            Companies
          </Link>
          <button
            onClick={() => signOut(auth)}
            className="text-[13px]"
            style={{ color: "var(--subtle)" }}
          >
            Sign out
          </button>
          <div
            className="h-7 w-7 rounded-full border"
            style={{ background: "var(--surface-2)", borderColor: "var(--border)" }}
          />
        </div>
      ) : (
        <Link
          href="/"
          className="text-[13px] font-medium"
          style={{ color: "var(--accent)" }}
        >
          ← Back to jobs
        </Link>
      )}
    </header>
  );
}
