// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import { copy } from "./copy";

const DOTS = ["var(--terracotta)", "var(--sage)", "var(--honey)"];

/** The three one-line promises under the hero. */
export function ProofLines() {
  return (
    <div
      style={{
        background: "var(--surface-warm)",
        borderTop: "1px solid var(--border-warm-hair)",
        borderBottom: "1px solid var(--border-warm-hair)",
      }}
    >
      <div
        style={{
          maxWidth: 1140,
          margin: "0 auto",
          padding: "40px 28px",
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit,minmax(240px,1fr))",
          gap: 26,
        }}
      >
        {copy.proof.map((line, i) => (
          <div key={line.strong} style={{ display: "flex", gap: 13, alignItems: "flex-start" }}>
            <span
              aria-hidden="true"
              style={{
                width: 9,
                height: 9,
                borderRadius: "50%",
                background: DOTS[i],
                marginTop: 7,
                flex: "none",
              }}
            />
            <span style={{ fontSize: 14.5, lineHeight: 1.6, color: "var(--ink-2)" }}>
              {line.before}
              <b style={{ color: "var(--ink)" }}>{line.strong}</b>
              {line.after}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
