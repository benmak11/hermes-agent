// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

export type TileHue = "violet" | "honey" | "sage" | "terracotta";

const HUES: Record<TileHue, { bg: string; fg: string }> = {
  violet: { bg: "#f3edf7", fg: "#7a5a94" },
  honey: { bg: "var(--honey-tint)", fg: "#9a6216" },
  sage: { bg: "var(--sage-tint)", fg: "var(--sage)" },
  terracotta: { bg: "var(--terracotta-tint)", fg: "var(--terracotta)" },
};

/** Company monogram square used in place of a logo. */
export function CompanyTile({
  initial,
  hue,
}: {
  initial: string;
  hue: TileHue;
}) {
  const { bg, fg } = HUES[hue];
  return (
    <span
      aria-hidden="true"
      style={{
        width: 34,
        height: 34,
        borderRadius: 11,
        background: bg,
        color: fg,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 14,
        fontWeight: 800,
        flex: "none",
      }}
    >
      {initial}
    </span>
  );
}
