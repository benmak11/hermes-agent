// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

import type { CSSProperties } from "react";

import { CompanyTile, type TileHue } from "@/components/warm/CompanyTile";
import { MatchChip } from "@/components/warm/MatchChip";
import { Pill } from "@/components/warm/Pill";
import { copy } from "./copy";
import { EYEBROW, H2, SECTION } from "./styles";

/**
 * The three beats. Each mock panel is an illustration (`aria-hidden`); the
 * paragraph beside it carries the meaning. Beat 2 flips its columns with
 * `order`, which also puts its mock above the text in the single-column
 * reflow — that is the design.
 */

const CARD: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit,minmax(300px,1fr))",
  gap: 34,
  alignItems: "center",
  borderRadius: 24,
  border: "1px solid var(--border-warm-hair)",
  background: "var(--surface-warm)",
  padding: 38,
  boxShadow: "0 1px 2px rgba(94,63,39,0.04),0 16px 40px rgba(94,63,39,0.07)",
};

const H3: CSSProperties = {
  margin: "16px 0 0",
  fontSize: 24,
  fontWeight: 800,
  letterSpacing: "-0.01em",
  color: "var(--ink)",
};

const P: CSSProperties = {
  margin: "12px 0 0",
  fontSize: 15,
  lineHeight: 1.65,
  color: "var(--ink-3)",
  textWrap: "pretty",
};

const MOCK: CSSProperties = {
  borderRadius: 16,
  border: "1px solid var(--border-warm-hair)",
  background: "#fdf7ee",
  padding: 18,
};

const MOCK_LABEL: CSSProperties = {
  fontSize: 11,
  fontWeight: 800,
  letterSpacing: "0.12em",
  textTransform: "uppercase",
  color: "var(--ink-4)",
};

function BeatText({ beat }: { beat: (typeof copy.how.beats)[number] }) {
  return (
    <>
      <Pill tone="accent">{beat.pill}</Pill>
      <h3 style={H3}>{beat.h3}</h3>
      <p style={P}>{beat.p}</p>
    </>
  );
}

const MOCK1_TILES: { hue: TileHue; initial: string }[] = [
  { hue: "honey", initial: "A" },
  { hue: "violet", initial: "S" },
];

function Mock1() {
  const m = copy.how.mock1;
  return (
    <div aria-hidden="true" style={MOCK}>
      {m.rows.map((row, i) => (
        <div
          key={row.title}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 11,
            padding: "13px 15px",
            borderRadius: 12,
            background: "var(--surface-warm)",
            border: "1px solid #e8dacb",
            marginBottom: i === m.rows.length - 1 ? 14 : 9,
          }}
        >
          <CompanyTile size="sm" hue={MOCK1_TILES[i].hue} initial={MOCK1_TILES[i].initial} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13.5, fontWeight: 700, color: "var(--ink)" }}>{row.title}</div>
            <div style={{ fontSize: 12, color: "var(--ink-4)" }}>{row.sub}</div>
          </div>
          <MatchChip label={row.chip} />
        </div>
      ))}
      <div style={{ display: "flex", gap: 9 }}>
        <span
          style={{
            flex: 1,
            height: 38,
            borderRadius: 11,
            background: "var(--terracotta)",
            color: "#fff9f2",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 13,
            fontWeight: 700,
          }}
        >
          {m.apply}
        </span>
        <span
          style={{
            height: 38,
            padding: "0 15px",
            borderRadius: 11,
            background: "var(--surface-warm)",
            border: "1px solid #e8dacb",
            color: "var(--ink-4)",
            display: "flex",
            alignItems: "center",
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          {m.skip}
        </span>
      </div>
    </div>
  );
}

const MOCK2_CHIPS: { bg: string; fg: string }[] = [
  { bg: "#f3edf7", fg: "#7a5a94" },
  { bg: "var(--honey-tint)", fg: "#9a6216" },
  { bg: "var(--sage-tint)", fg: "var(--sage)" },
];

function Mock2() {
  const m = copy.how.mock2;
  return (
    <div aria-hidden="true" style={{ ...MOCK, order: 1 }}>
      <div style={{ ...MOCK_LABEL, marginBottom: 12 }}>{m.label}</div>
      {m.rows.map((row, i) => (
        <div
          key={row.name}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 11,
            padding: "12px 14px",
            borderRadius: 11,
            background: "var(--surface-warm)",
            border: "1px solid var(--border-warm)",
            marginBottom: i === m.rows.length - 1 ? 0 : 8,
          }}
        >
          <span aria-hidden="true" style={{ color: "#c2ab92", fontSize: 13 }}>
            ⠿
          </span>
          <span style={{ flex: 1, minWidth: 0, fontSize: 13, fontWeight: 700, color: "var(--ink)" }}>
            {row.name}
          </span>
          <span
            style={{
              padding: "3px 9px",
              borderRadius: 8,
              background: MOCK2_CHIPS[i].bg,
              fontSize: 11,
              fontWeight: 700,
              color: MOCK2_CHIPS[i].fg,
              flex: "none",
            }}
          >
            {row.chip}
          </span>
        </div>
      ))}
    </div>
  );
}

const MOCK3_BARS: { width: string; fill: string; count: string; label: CSSProperties }[] = [
  { width: "100%", fill: "#7d9b6f", count: "var(--sage)", label: { fontWeight: 600, color: "var(--ink-2)" } },
  { width: "40%", fill: "var(--honey)", count: "#9a6216", label: { fontWeight: 600, color: "var(--ink-2)" } },
  { width: "33%", fill: "var(--terracotta)", count: "var(--brick)", label: { fontWeight: 800, color: "var(--brick)" } },
];

function Mock3() {
  const m = copy.how.mock3;
  return (
    <div aria-hidden="true" style={MOCK}>
      <div style={{ ...MOCK_LABEL, marginBottom: 14 }}>{m.label}</div>
      {m.rows.map((row, i) => (
        <div
          key={row.name}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            marginBottom: i === m.rows.length - 1 ? 0 : 10,
          }}
        >
          <span style={{ width: 108, flex: "none", fontSize: 12.5, ...MOCK3_BARS[i].label }}>
            {row.name}
          </span>
          <div
            style={{
              flex: 1,
              minWidth: 0,
              height: 22,
              borderRadius: 7,
              background: "#f4ebdf",
              overflow: "hidden",
            }}
          >
            <div style={{ width: MOCK3_BARS[i].width, height: "100%", background: MOCK3_BARS[i].fill }} />
          </div>
          <span
            style={{
              width: 34,
              flex: "none",
              fontSize: 12,
              fontWeight: 700,
              color: MOCK3_BARS[i].count,
              textAlign: "right",
            }}
          >
            {row.count}
          </span>
        </div>
      ))}
      <p style={{ margin: "14px 0 0", fontSize: 12.5, lineHeight: 1.6, color: "var(--ink-3)" }}>
        {m.note}
      </p>
    </div>
  );
}

export function HowItWorks() {
  const [b1, b2, b3] = copy.how.beats;
  return (
    <section id="how" style={SECTION}>
      <div style={{ maxWidth: 620 }}>
        <span style={EYEBROW}>{copy.how.eyebrow}</span>
        <h2 style={H2}>{copy.how.h2}</h2>
      </div>
      <div style={{ marginTop: 44, display: "flex", flexDirection: "column", gap: 20 }}>
        <div style={CARD}>
          <div>
            <BeatText beat={b1} />
          </div>
          <Mock1 />
        </div>
        <div style={CARD}>
          <div style={{ order: 2 }}>
            <BeatText beat={b2} />
          </div>
          <Mock2 />
        </div>
        <div style={CARD}>
          <div>
            <BeatText beat={b3} />
          </div>
          <Mock3 />
        </div>
      </div>
    </section>
  );
}
