// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import { SERIF } from "./styles";

/** Heading + one paragraph — the body of `/security`, `/contact`, `/terms` until they are designed. */
export function PlaceholderPage({ h1, p }: { h1: string; p: string }) {
  return (
    <section style={{ maxWidth: 720, margin: "0 auto", padding: "96px 28px 120px" }}>
      <h1
        style={{
          margin: 0,
          fontFamily: SERIF,
          fontWeight: 400,
          fontSize: "clamp(30px,4.2vw,46px)",
          lineHeight: 1.1,
          letterSpacing: "-0.015em",
          color: "var(--ink)",
        }}
      >
        {h1}
      </h1>
      <p
        style={{
          margin: "18px 0 0",
          fontSize: 16,
          lineHeight: 1.65,
          color: "var(--ink-3)",
          textWrap: "pretty",
        }}
      >
        {p}
      </p>
    </section>
  );
}
