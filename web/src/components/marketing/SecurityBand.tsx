// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import { copy } from "./copy";
import { EYEBROW, SECTION, SERIF } from "./styles";

/** The dark ink band. Its text ramp (#e0a47f / #d6c3b0 / #f0e2d2) has no tokens. */
export function SecurityBand() {
  const s = copy.security;
  return (
    <section id="security" style={SECTION}>
      <div
        style={{
          borderRadius: 26,
          background: "var(--ink)",
          padding: "clamp(34px,5vw,58px)",
          color: "var(--sand)",
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit,minmax(280px,1fr))",
          gap: 40,
          alignItems: "center",
        }}
      >
        <div>
          <span style={{ ...EYEBROW, color: "#e0a47f" }}>{s.eyebrow}</span>
          <h2
            style={{
              margin: "16px 0 0",
              fontFamily: SERIF,
              fontWeight: 400,
              fontSize: "clamp(28px,3.6vw,40px)",
              lineHeight: 1.12,
              color: "var(--cream)",
            }}
          >
            {s.h2}
          </h2>
          <p
            style={{
              margin: "16px 0 0",
              fontSize: 15,
              lineHeight: 1.65,
              color: "#d6c3b0",
              maxWidth: 420,
              textWrap: "pretty",
            }}
          >
            {s.p}
          </p>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 13 }}>
          {s.points.map((point) => (
            <div
              key={point.strong}
              style={{
                display: "flex",
                gap: 13,
                alignItems: "flex-start",
                padding: "16px 18px",
                borderRadius: 14,
                background: "rgba(247,237,224,0.06)",
                border: "1px solid rgba(247,237,224,0.12)",
              }}
            >
              <span aria-hidden="true" style={{ color: "#e0a47f", fontSize: 14, flex: "none" }}>
                ✓
              </span>
              <span style={{ fontSize: 14, lineHeight: 1.55, color: "#f0e2d2" }}>
                <b style={{ color: "var(--cream)" }}>{point.strong}</b>
                {point.rest}
              </span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
