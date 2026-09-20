// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth";
import { loginHref } from "@/lib/nav";

/**
 * The one client-side auth gate for everything under /app. Replaces the
 * per-page redirect-to-login effects. While Firebase is still resolving
 * the session, children render (each page owns its own skeleton); a resolved
 * signed-out visitor is sent to /login with a `?next=` back to here.
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
    return <div className="p-8" style={{ color: "var(--muted)" }}>Loading…</div>;
  }
  return <>{children}</>;
}
