// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import Link from "next/link";

import { copy } from "./copy";
import { LINKS } from "./links";
import { SECTION, SERIF } from "./styles";

/** Closing card; `id="start"` is the footer's target. */
export function ClosingCta() {
  const c = copy.closing;
  return (
    <section id="start" style={SECTION}>
      <div
        style={{
          position: "relative",
          overflow: "hidden",
          borderRadius: 26,
          border: "1px solid var(--border-warm)",
          background:
            "radial-gradient(700px 320px at 50% 0%, var(--terracotta-tint) 0%, var(--surface-warm) 70%)",
          padding: "clamp(44px,6vw,76px) 28px",
          textAlign: "center",
        }}
      >
        <h2
          style={{
            margin: 0,
            fontFamily: SERIF,
            fontWeight: 400,
            fontSize: "clamp(32px,4.8vw,54px)",
            lineHeight: 1.08,
            letterSpacing: "-0.015em",
            color: "var(--ink)",
            textWrap: "balance",
          }}
        >
          {c.h2}
        </h2>
        <p
          style={{
            margin: "18px auto 0",
            maxWidth: 460,
            fontSize: 16,
            lineHeight: 1.6,
            color: "var(--ink-3)",
          }}
        >
          {c.p}
        </p>
        <Link
          href={LINKS.signup.closing}
          className="mk-cta"
          style={{
            marginTop: 28,
            display: "inline-flex",
            alignItems: "center",
            height: 52,
            padding: "0 30px",
            borderRadius: 14,
            fontSize: 16,
            fontWeight: 700,
            boxShadow: "0 10px 26px rgba(184,83,47,0.28)",
          }}
        >
          {c.cta}
        </Link>
      </div>
    </section>
  );
}
