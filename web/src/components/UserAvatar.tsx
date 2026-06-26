// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useAuth } from "@/lib/auth";
import { resolveUserAvatar } from "@/lib/ui";

/**
 * The signed-in user's avatar, resolved from their identity (onboarding
 * "Avatar resolution" spec): a name → initials, else the email's first letter,
 * else a neutral person glyph. Falls back to the glyph while auth is loading.
 */
export function UserAvatar({ className = "h-7 w-7" }: { className?: string }) {
  const { user } = useAuth();
  const av = resolveUserAvatar(user?.displayName, user?.email);
  const label = user?.displayName || user?.email || "Account";

  if (av.kind === "glyph") {
    return (
      <span
        className={`${className} flex items-center justify-center rounded-full border`}
        style={{
          background: "var(--surface-2)",
          borderColor: "var(--border)",
          color: "var(--muted)",
        }}
        aria-label={label}
        title={label}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="12" cy="8.4" r="3.5" fill="currentColor" />
          <path d="M5.4 19.2c0-3.7 3-6.1 6.6-6.1s6.6 2.4 6.6 6.1" fill="currentColor" />
        </svg>
      </span>
    );
  }

  return (
    <span
      className={`${className} flex items-center justify-center rounded-full text-[11px] font-bold`}
      style={{ background: "var(--text)", color: "var(--surface)" }}
      aria-label={label}
      title={label}
    >
      {av.text}
    </span>
  );
}
