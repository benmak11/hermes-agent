// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { redirect } from "next/navigation";

// The applications list lives at /tracking now (seamless-journey mock 04);
// the per-application review page below this route is unchanged.
export default function ApplicationsPage() {
  redirect("/tracking");
}
