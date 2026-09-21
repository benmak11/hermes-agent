// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import { GROUND } from "@/components/warm/styles";

// Shared ground for `/login`, `/signup`, `/onboarding`, `/onboarding/review`.
// Nests inside the root layout (so `Providers` still wraps it); no metadata,
// the root's applies. A server component — the pages keep their own Suspense
// boundaries around `useSearchParams`.
export default function AuthLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    // `.wm` is now just a marker (its light-only ground rule in globals.css
    // went in PR 8 — body is sand everywhere).
    // `GROUND`'s `flex: 1 0 auto` fills the root body.min-h-full.flex.flex-col;
    // the pages' <main className="flex flex-1 ..."> centre inside this column.
    <div className="wm" style={GROUND}>
      {children}
    </div>
  );
}
