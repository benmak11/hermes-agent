// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import { SANS } from "@/components/warm/styles";

// Shared ground for `/login`, `/signup`, `/onboarding`, `/onboarding/review`.
// Nests inside the root layout (so `Providers` still wraps it); no metadata,
// the root's applies. A server component — the pages keep their own Suspense
// boundaries around `useSearchParams`.
export default function AuthLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    // `.wm` keys the `html:has(.mk, .wm)` light-only ground rule in globals.css.
    // `flex: 1 0 auto` fills the root body.min-h-full.flex.flex-col; the pages'
    // <main className="flex flex-1 ..."> centre inside this column.
    <div
      className="wm"
      style={{
        fontFamily: SANS,
        color: "var(--ink)",
        background: "radial-gradient(1100px 620px at 50% -8%, var(--cream) 0%, #f1e4d3 70%)",
        colorScheme: "light",
        flex: "1 0 auto",
        width: "100%",
        display: "flex",
        flexDirection: "column",
      }}
    >
      {children}
    </div>
  );
}
