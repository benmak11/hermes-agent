// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import { ClosingCta } from "@/components/marketing/ClosingCta";
import { FeatureGrid } from "@/components/marketing/FeatureGrid";
import { Hero } from "@/components/marketing/Hero";
import { HowItWorks } from "@/components/marketing/HowItWorks";
import { ProofLines } from "@/components/marketing/ProofLines";
import { SecurityBand } from "@/components/marketing/SecurityBand";

// Marketing home (facelift PR 3): "Hermes Website" design. Static — the hero
// is demo data through the shared JourneyTrack; the nav and footer come from
// the (marketing) layout. Sets no title, so it gets the layout's default.
export default function Home() {
  return (
    <>
      <Hero />
      <ProofLines />
      <HowItWorks />
      <FeatureGrid />
      <SecurityBand />
      <ClosingCta />
    </>
  );
}
