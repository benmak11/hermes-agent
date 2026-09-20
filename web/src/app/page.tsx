// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { redirect } from "next/navigation";

// Placeholder until the marketing page lands (facelift PR 3). Server-side:
// no auth knowledge here, /app's layout does the gating.
export default function Home() {
  redirect("/app");
}
