// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";

import { AuthCard } from "@/components/AuthCard";
import { writeStored } from "@/lib/localStore";
import { SIGNUP_FROM_KEY, signupFrom } from "@/lib/nav";

export default function SignupPage() {
  // useSearchParams needs a Suspense boundary for prerendering (same rule
  // tracking/page.tsx follows).
  return (
    <Suspense fallback={<main className="flex flex-1 items-center justify-center p-6" />}>
      <SignupInner />
    </Suspense>
  );
}

function SignupInner() {
  const params = useSearchParams();
  // Stash where the visitor came from (marketing CTA) for the waitlist/source
  // endpoint, which a later PR reads and clears. Only written when known — a
  // bare /signup must not erase a source captured earlier in this tab.
  const from = signupFrom(params.get("from"));
  useEffect(() => {
    if (from) writeStored("session", SIGNUP_FROM_KEY, from);
  }, [from]);
  // `next` is null on purpose: a freshly created account goes to /onboarding.
  return <AuthCard initialMode="signup" next={null} />;
}
