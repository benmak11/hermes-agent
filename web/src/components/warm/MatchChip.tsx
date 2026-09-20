// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/** Small sage "92% match" chip. */
export function MatchChip({ label }: { label: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "4px 10px",
        borderRadius: 9,
        fontSize: 11.5,
        fontWeight: 700,
        lineHeight: 1.3,
        color: "var(--sage)",
        background: "var(--sage-tint)",
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </span>
  );
}
