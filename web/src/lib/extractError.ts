// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { ApiError } from "@/lib/apiError";

export type ExtractCap = {
  reason: "extract_cap";
  used: number;
  per_day: number;
  resets_at: string;
};

/** Read the daily résumé-extraction 429 out of a failed upload, or null. */
export function extractCap(err: unknown): ExtractCap | null {
  if (!(err instanceof ApiError) || err.status !== 429) return null;
  try {
    const detail = (JSON.parse(err.body) as { detail?: unknown }).detail as
      | ExtractCap
      | undefined;
    if (detail?.reason !== "extract_cap") return null;
    if (typeof detail.used !== "number" || typeof detail.resets_at !== "string") return null;
    return detail;
  } catch {
    return null;
  }
}

/** "8:00 PM" in the viewer's timezone, or "tomorrow" if the instant is unreadable. */
function resetTime(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "tomorrow";
  return at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

/** What the onboarding upload shows when `POST /profile/extract` fails. */
export function extractErrorMessage(err: unknown): string {
  const cap = extractCap(err);
  if (cap) {
    const times = cap.used === 1 ? "once" : `${cap.used} times`;
    return `You've uploaded your résumé ${times} today; try again after ${resetTime(cap.resets_at)}.`;
  }
  return err instanceof Error
    ? err.message.replace(/^\d+:\s*/, "")
    : "Something went wrong reading your resume.";
}
