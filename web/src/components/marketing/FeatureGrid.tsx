// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import { copy } from "./copy";
import { EYEBROW, H2, SECTION } from "./styles";

const ICON_TINTS = [
  "var(--terracotta-tint)",
  "var(--sage-tint)",
  "var(--honey-tint)",
  "#f3edf7",
  "#fdf5f2",
  "#f6ede1",
];

/** Six feature cards; `id="journeys"` is the hero badge's and the dropdown's target. */
export function FeatureGrid() {
  const f = copy.features;
  return (
    <section id="journeys" style={SECTION}>
      <div style={{ maxWidth: 560 }}>
        <span style={EYEBROW}>{f.eyebrow}</span>
        <h2 style={H2}>{f.h2}</h2>
      </div>
      <div
        style={{
          marginTop: 36,
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit,minmax(272px,1fr))",
          gap: 18,
        }}
      >
        {f.items.map((item, i) => (
          <div
            key={item.h3}
            style={{
              borderRadius: 20,
              border: "1px solid var(--border-warm-hair)",
              background: "var(--surface-warm)",
              padding: 26,
            }}
          >
            <span
              aria-hidden="true"
              style={{
                width: 34,
                height: 34,
                borderRadius: 11,
                background: ICON_TINTS[i],
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 15,
              }}
            >
              {item.icon}
            </span>
            <h3 style={{ margin: "16px 0 0", fontSize: 17, fontWeight: 800, color: "var(--ink)" }}>
              {item.h3}
            </h3>
            <p style={{ margin: "9px 0 0", fontSize: 14, lineHeight: 1.6, color: "var(--ink-3)" }}>
              {item.p}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}
