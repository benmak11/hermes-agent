// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/** Color (a CSS var) for the big numeric score, keyed by recommendation. */
export function scoreColor(rec: string): string {
  if (rec === "strong_apply") return "var(--good)";
  if (rec === "apply") return "var(--accent)";
  if (rec === "maybe") return "var(--warn)";
  return "var(--subtle)";
}

export type Pill = {
  bg: string;
  border: string;
  color: string;
  dot: string;
  label: string;
};

/** Recommendation pill styling (CSS vars) + display label. */
export function recPill(rec: string): Pill {
  switch (rec) {
    case "strong_apply":
      return {
        bg: "var(--good-bg)",
        border: "var(--good-border)",
        color: "var(--good)",
        dot: "var(--good)",
        label: "STRONG APPLY",
      };
    case "apply":
      return {
        bg: "var(--accent-bg)",
        border: "var(--accent-border)",
        color: "var(--accent-text)",
        dot: "var(--accent)",
        label: "APPLY",
      };
    case "maybe":
      return {
        bg: "var(--warn-bg)",
        border: "var(--warn-border)",
        color: "var(--warn)",
        dot: "var(--warn)",
        label: "MAYBE",
      };
    default:
      return {
        bg: "var(--surface-2)",
        border: "var(--border)",
        color: "var(--muted)",
        dot: "var(--subtle)",
        label: "SKIP",
      };
  }
}

/** Fill color for a 0-100 score bar. */
export function barColor(v: number): string {
  if (v >= 80) return "var(--good)";
  if (v >= 60) return "var(--accent)";
  return "var(--warn)";
}

const AVATAR_COLORS: { bg: string; color: string }[] = [
  { bg: "#eef2ff", color: "#4f46e5" },
  { bg: "#ecfeff", color: "#0891b2" },
  { bg: "#f0fdf4", color: "#16a34a" },
  { bg: "#fff7ed", color: "#ea580c" },
  { bg: "#faf5ff", color: "#9333ea" },
  { bg: "#fef2f2", color: "#e11d48" },
];

/** Deterministic brand-ish avatar color from a company name/slug. */
export function avatarColor(seed: string): { bg: string; color: string } {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

export function initial(name: string): string {
  return (name.trim()[0] ?? "?").toUpperCase();
}

export type UserAvatar =
  | { kind: "initials"; text: string }
  | { kind: "email"; text: string }
  | { kind: "glyph" };

/**
 * Resolve a signed-in user's avatar: a name yields initials (first + last),
 * otherwise the email's first letter, otherwise a neutral person glyph.
 * Mirrors the onboarding "Avatar resolution" spec:
 *   initials(name) ?? email[0].toUpperCase() ?? glyph
 */
export function resolveUserAvatar(
  name?: string | null,
  email?: string | null,
): UserAvatar {
  const parts = (name ?? "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 1) {
    return { kind: "initials", text: parts[0][0].toUpperCase() };
  }
  if (parts.length >= 2) {
    const first = parts[0][0];
    const last = parts[parts.length - 1][0];
    return { kind: "initials", text: (first + last).toUpperCase() };
  }
  const e = email?.trim();
  if (e) return { kind: "email", text: e[0].toUpperCase() };
  return { kind: "glyph" };
}
