// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import Link from "next/link";

import { CompanyTile } from "@/components/warm/CompanyTile";
import { JourneyTrack } from "@/components/warm/JourneyTrack";
import { Pill } from "@/components/warm/Pill";
import { copy } from "./copy";
import { HERO_ROW, HERO_STAGES } from "./heroDemo";
import { LINKS } from "./links";
import { SERIF } from "./styles";

/** Hero text + the app-frame mock whose journey track is the shared component on demo data. */
export function Hero() {
  const h = copy.hero;
  return (
    <section style={{ position: "relative", padding: "76px 28px 0" }}>
      <div
        aria-hidden="true"
        style={{
          position: "absolute",
          inset: 0,
          background:
            "radial-gradient(900px 480px at 50% -10%, var(--cream) 0%, rgba(253,246,234,0) 72%)",
          pointerEvents: "none",
        }}
      />
      <div
        style={{
          position: "relative",
          maxWidth: 1000,
          margin: "0 auto",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          textAlign: "center",
        }}
      >
        <Link
          href={LINKS.journeys}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 9,
            padding: "6px 7px 6px 13px",
            borderRadius: 999,
            background: "var(--surface-warm)",
            border: "1px solid #e8dacb",
            fontSize: 13,
            fontWeight: 600,
            color: "#5c4a3b",
          }}
        >
          <span style={{ color: "var(--terracotta)", fontWeight: 800 }}>{h.badgeNew}</span>
          {h.badge}
          <span
            aria-hidden="true"
            style={{
              width: 22,
              height: 22,
              borderRadius: "50%",
              background: "var(--sand)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 11,
              color: "var(--ink-3)",
            }}
          >
            →
          </span>
        </Link>
        <h1
          style={{
            margin: "26px 0 0",
            fontFamily: SERIF,
            fontWeight: 400,
            fontSize: "clamp(40px,6.4vw,76px)",
            lineHeight: 1.02,
            letterSpacing: "-0.02em",
            color: "var(--ink)",
            textWrap: "balance",
          }}
        >
          {h.h1Line1}
          <br />
          <em style={{ fontStyle: "italic", color: "var(--terracotta)" }}>{h.h1Em}</em>
          {h.h1Line2Rest}
        </h1>
        <p
          style={{
            margin: "22px 0 0",
            maxWidth: 560,
            fontSize: "clamp(15px,1.7vw,18px)",
            lineHeight: 1.6,
            color: "var(--ink-3)",
            textWrap: "pretty",
          }}
        >
          {h.lede}
        </p>
        <div
          style={{
            marginTop: 30,
            display: "flex",
            flexWrap: "wrap",
            gap: 12,
            justifyContent: "center",
          }}
        >
          <Link
            href={LINKS.signup.hero}
            className="mk-cta"
            style={{
              display: "flex",
              alignItems: "center",
              height: 50,
              padding: "0 26px",
              borderRadius: 14,
              fontSize: 15.5,
              fontWeight: 700,
              boxShadow: "0 10px 24px rgba(184,83,47,0.26)",
            }}
          >
            {h.cta}
          </Link>
          <Link
            href={LINKS.how}
            className="mk-ghost"
            style={{
              display: "flex",
              alignItems: "center",
              height: 50,
              padding: "0 24px",
              borderRadius: 14,
              border: "1px solid #e8dacb",
              color: "var(--ink-2)",
              fontSize: 15.5,
              fontWeight: 600,
            }}
          >
            {h.secondary}
          </Link>
        </div>
        <p style={{ margin: "16px 0 0", fontSize: 13, color: "var(--ink-4)" }}>{h.subline}</p>
      </div>

      <div style={{ position: "relative", maxWidth: 1080, margin: "56px auto 0" }}>
        <div
          style={{
            borderRadius: "22px 22px 0 0",
            border: "1px solid var(--border-warm-hair)",
            borderBottom: "none",
            background: "var(--surface-warm)",
            boxShadow: "0 -1px 2px rgba(94,63,39,0.04),0 28px 70px rgba(94,63,39,0.16)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              height: 52,
              borderBottom: "1px solid #f2e6d7",
              display: "flex",
              alignItems: "center",
              gap: 9,
              padding: "0 20px",
            }}
          >
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                aria-hidden="true"
                style={{ width: 10, height: 10, borderRadius: "50%", background: "#efd9c9" }}
              />
            ))}
            <span
              style={{ marginLeft: 10, fontSize: 12.5, fontWeight: 700, color: "var(--ink-3)" }}
            >
              {h.demo.frameTitle}
            </span>
            <span style={{ flex: 1 }} />
            <Pill tone="accent">{h.demo.live}</Pill>
          </div>
          <div style={{ padding: "26px 26px 34px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
              <CompanyTile initial={HERO_ROW.initial} hue={HERO_ROW.hue} />
              <span style={{ fontSize: 15, fontWeight: 700, color: "var(--ink)" }}>
                {h.demo.role}{" "}
                <span style={{ fontWeight: 400, color: "var(--ink-4)" }}>{h.demo.company}</span>
              </span>
              <span style={{ flex: 1 }} />
              <Pill tone="accent">{h.demo.pill}</Pill>
            </div>
            <JourneyTrack stages={[...HERO_STAGES]} layout="fluid" />
          </div>
        </div>
      </div>
    </section>
  );
}
