// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { AuthCard } from "@/components/AuthCard";

export default function LoginPage() {
  // useSearchParams needs a Suspense boundary for prerendering (same rule
  // tracking/page.tsx follows).
  return (
    <Suspense fallback={<main className="flex flex-1 items-center justify-center p-6" />}>
      <LoginInner />
    </Suspense>
  );
}

function LoginInner() {
  const params = useSearchParams();
  return <AuthCard initialMode="signin" next={params.get("next")} />;
}
