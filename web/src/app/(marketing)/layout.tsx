// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { Metadata } from "next";

import { Footer } from "@/components/marketing/Footer";
import { MarketingNav } from "@/components/marketing/MarketingNav";
import { copy } from "@/components/marketing/copy";
import { SANS } from "@/components/marketing/styles";

// Shared chrome for `/`, `/security`, `/contact`, `/terms`. Nests inside the
// root layout, so `Providers` still wraps it and the nav's `useAuth` has its
// provider. `title.template` applies to the child routes' own titles; `/`
// sets none and gets the default.
export const metadata: Metadata = {
  title: { default: copy.meta.title, template: `%s — ${copy.nav.brand}` },
  description: copy.meta.description,
};

export default function MarketingLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    // `overflowX: "clip"`, never `hidden`: `hidden` would make this the
    // scroll container and the sticky nav would stick to a box as tall as
    // the page. `.mk` keys the `html:has(.mk)` ground rule in globals.css.
    // `flex: 1 0 auto` fills the root `body.min-h-full.flex.flex-col`.
    <div
      id="top"
      className="mk"
      style={{
        fontFamily: SANS,
        color: "var(--ink)",
        background: "var(--sand)",
        colorScheme: "light",
        flex: "1 0 auto",
        width: "100%",
        overflowX: "clip",
      }}
    >
      <MarketingNav />
      <main>{children}</main>
      <Footer />
    </div>
  );
}
