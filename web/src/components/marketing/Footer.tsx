// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import Link from "next/link";
import type { CSSProperties } from "react";

import { copy } from "./copy";
import { LINKS } from "./links";

/**
 * Static footer, rendered by the marketing layout on every marketing route.
 * There is no signed-in variant (none is designed): a signed-in visitor sees
 * "Request an invite / Sign in" here too.
 */

const COLUMN: CSSProperties = { display: "flex", flexDirection: "column", gap: 10 };

const HEADING: CSSProperties = {
  fontSize: 11.5,
  fontWeight: 800,
  letterSpacing: "0.12em",
  textTransform: "uppercase",
  color: "var(--ink-4)",
};

// Colour lives on .mk-link so :hover can override it (inline would win).
const LINK: CSSProperties = { fontSize: 13.5 };

export function Footer() {
  const f = copy.footer;
  return (
    <footer style={{ marginTop: 80, borderTop: "1px solid #e8dacb" }}>
      <div
        style={{
          maxWidth: 1140,
          margin: "0 auto",
          padding: "44px 28px 56px",
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))",
          gap: 34,
          alignItems: "start",
        }}
      >
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
            <span
              aria-hidden="true"
              style={{
                width: 28,
                height: 28,
                borderRadius: 9,
                background: "var(--terracotta)",
                color: "#fff9f2",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontWeight: 800,
                fontSize: 14,
              }}
            >
              {copy.nav.brand.charAt(0)}
            </span>
            <span style={{ fontSize: 16, fontWeight: 800, color: "var(--ink)" }}>
              {copy.nav.brand}
            </span>
          </div>
          <p
            style={{
              margin: "14px 0 0",
              fontSize: 13,
              lineHeight: 1.6,
              color: "var(--ink-3)",
              maxWidth: 220,
            }}
          >
            {f.tagline}
          </p>
        </div>
        <div style={COLUMN}>
          <span style={HEADING}>{f.product.heading}</span>
          <Link href={LINKS.how} className="mk-link" style={LINK}>
            {f.product.howItWorks}
          </Link>
          <Link href={LINKS.journeys} className="mk-link" style={LINK}>
            {f.product.journeys}
          </Link>
          <Link href={LINKS.journeys} className="mk-link" style={LINK}>
            {f.product.insights}
          </Link>
        </div>
        <div style={COLUMN}>
          <span style={HEADING}>{f.company.heading}</span>
          <Link href={LINKS.pages.security} className="mk-link" style={LINK}>
            {f.company.security}
          </Link>
          <Link href={LINKS.pages.contact} className="mk-link" style={LINK}>
            {f.company.contact}
          </Link>
          <Link href={LINKS.pages.terms} className="mk-link" style={LINK}>
            {f.company.terms}
          </Link>
        </div>
        <div style={COLUMN}>
          <span style={HEADING}>{f.start.heading}</span>
          <Link
            href={LINKS.signup.footer}
            className="mk-cta"
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              height: 40,
              padding: "0 18px",
              borderRadius: 11,
              fontSize: 13.5,
              fontWeight: 700,
            }}
          >
            {f.start.cta}
          </Link>
          <Link href={LINKS.login} className="mk-link" style={LINK}>
            {f.start.signIn}
          </Link>
        </div>
      </div>
      <div
        style={{
          maxWidth: 1140,
          margin: "0 auto",
          padding: "0 28px 40px",
          fontSize: 12.5,
          color: "var(--ink-4)",
        }}
      >
        {f.copyright}
      </div>
    </footer>
  );
}
