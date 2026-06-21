// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import type { ApplicationStatus } from "@/lib/types";

export type StatusPill = {
  label: string;
  bg: string;
  border: string;
  color: string;
};

/** Visual treatment for each application status, reusing the theme tokens. */
export function statusPill(status: ApplicationStatus): StatusPill {
  switch (status) {
    case "ready_for_review":
      return {
        label: "ready for review",
        bg: "var(--good-bg)",
        border: "var(--good-border)",
        color: "var(--good)",
      };
    case "submitted":
    case "responded":
      return {
        label: status,
        bg: "var(--good-bg)",
        border: "var(--good-border)",
        color: "var(--good)",
      };
    case "failed":
      return {
        label: "failed",
        bg: "var(--warn-bg)",
        border: "var(--warn-border)",
        color: "var(--danger)",
      };
    case "tailoring":
    case "submitting":
    case "queued":
    default:
      return {
        label: status,
        bg: "var(--surface-2)",
        border: "var(--border)",
        color: "var(--muted)",
      };
  }
}
