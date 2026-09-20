// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { Metadata } from "next";

import { PlaceholderPage } from "@/components/marketing/PlaceholderPage";
import { copy } from "@/components/marketing/copy";

const page = copy.pages.terms;

export const metadata: Metadata = { title: page.title, description: page.p };

export default function TermsPage() {
  return <PlaceholderPage h1={page.h1} p={page.p} />;
}
