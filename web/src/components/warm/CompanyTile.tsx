// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

export type TileHue = "violet" | "honey" | "sage" | "terracotta";

const HUES: Record<TileHue, { bg: string; fg: string }> = {
  violet: { bg: "#f3edf7", fg: "#7a5a94" },
  honey: { bg: "var(--honey-tint)", fg: "#9a6216" },
  sage: { bg: "var(--sage-tint)", fg: "var(--sage)" },
  terracotta: { bg: "var(--terracotta-tint)", fg: "var(--terracotta)" },
};

export type TileSize = "sm" | "md";

/** `md` is the board's 34px tile; `sm` the 28px one in list rows. */
const SIZES: Record<TileSize, { box: number; radius: number; font: number }> = {
  md: { box: 34, radius: 11, font: 14 },
  sm: { box: 28, radius: 9, font: 12.5 },
};

/** Company monogram square used in place of a logo. */
export function CompanyTile({
  initial,
  hue,
  size = "md",
}: {
  initial: string;
  hue: TileHue;
  size?: TileSize;
}) {
  const { bg, fg } = HUES[hue];
  const { box, radius, font } = SIZES[size];
  return (
    <span
      aria-hidden="true"
      style={{
        width: box,
        height: box,
        borderRadius: radius,
        background: bg,
        color: fg,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: font,
        fontWeight: 800,
        flex: "none",
      }}
    >
      {initial}
    </span>
  );
}
