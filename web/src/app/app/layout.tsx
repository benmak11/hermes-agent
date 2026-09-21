// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth";
import { loginHref } from "@/lib/nav";
import { GROUND } from "@/components/warm/styles";

/**
 * The one client-side auth gate for everything under /app. Replaces the
 * per-page redirect-to-login effects. While Firebase is still resolving
 * the session, children render (each page owns its own skeleton); a resolved
 * signed-out visitor is sent to /login with a `?next=` back to here.
 *
 * Also paints the warm ground (facelift PR 8): every /app page renders inside
 * one `.wm` flex column, so pages no longer wrap themselves in `GROUND`.
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (loading || user) return;
    // window is safe here: effects never run during prerender. Reading
    // location.search directly avoids useSearchParams, which would force a
    // Suspense boundary above every app page (use-search-params.md:181).
    router.replace(loginHref(pathname, window.location.search));
  }, [loading, user, pathname, router]);

  if (!loading && !user) {
    return (
      <div className="wm" style={GROUND}>
        <div className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>Loading…</div>
      </div>
    );
  }
  return <div className="wm" style={GROUND}>{children}</div>;
}
