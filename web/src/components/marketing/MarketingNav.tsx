// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import Link from "next/link";
import { useRef, useState, type CSSProperties } from "react";

import { useAuth } from "@/lib/authContext";
import { copy } from "./copy";
import { LINKS } from "./links";

/**
 * Sticky marketing header — the one client island on the site. Owns the
 * Features dropdown (`open` is the single source of truth: it drives
 * `aria-expanded`, `inert` on the panel, and the fade/slide) and the
 * signed-in swap, where "Sign in / Request an invite" becomes "Open Hermes →".
 *
 * SSR and the first client paint always render signed-out (the context's
 * initial value is `{user: null, loading: true}`), so the static HTML carries
 * the CTAs; a signed-in visitor sees them for the moment before
 * `onIdTokenChanged` resolves.
 */

// Colour lives on .mk-link so :hover can override it (inline would win).
const TEXT_LINK: CSSProperties = { fontSize: 14, fontWeight: 600 };

const CTA: CSSProperties = {
  display: "flex",
  alignItems: "center",
  height: 38,
  padding: "0 17px",
  borderRadius: 11,
  fontSize: 14,
  fontWeight: 700,
};

const ITEM_TINTS = ["var(--terracotta-tint)", "var(--sage-tint)", "var(--honey-tint)"];
const ITEM_ICONS = ["◷", "⌁", "◐"];
const ITEM_HREFS = [LINKS.journeys, LINKS.how, LINKS.journeys];

const PANEL_ID = "mk-features-panel";

function FeaturesMenu() {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const inside = (n: EventTarget | null) => !!n && !!wrapRef.current?.contains(n as Node);

  return (
    <div
      ref={wrapRef}
      className="mk-menu mk-desktop-only"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      // Bubbling focus/blur: open when focus enters from outside the wrapper,
      // close when it leaves it (Tab into the trigger opens; Tab past closes).
      onFocus={(e) => {
        if (!inside(e.relatedTarget)) setOpen(true);
      }}
      onBlur={(e) => {
        if (!inside(e.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.preventDefault();
          setOpen(false);
          triggerRef.current?.focus();
        }
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        aria-expanded={open}
        aria-controls={PANEL_ID}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          height: 66,
          padding: 0,
          border: 0,
          background: "none",
          font: "inherit",
          fontSize: 14,
          fontWeight: 600,
          color: "#5c4a3b",
          cursor: "pointer",
        }}
      >
        {copy.nav.features}
        <span aria-hidden="true" style={{ fontSize: 10, color: "var(--ink-3)" }}>
          ▾
        </span>
      </button>
      <div
        id={PANEL_ID}
        className="mk-menu-panel"
        inert={!open}
        style={{
          position: "absolute",
          top: 58,
          left: "50%",
          width: 496,
          marginLeft: -190,
          padding: 10,
          borderRadius: 18,
          background: "var(--surface-warm)",
          border: "1px solid #e8dacb",
          boxShadow: "0 18px 44px rgba(94,63,39,0.18)",
          display: "flex",
          flexDirection: "column",
          gap: 4,
          opacity: open ? 1 : 0,
          transform: open ? "translateY(0)" : "translateY(-6px)",
          pointerEvents: open ? "auto" : "none",
        }}
      >
        {copy.nav.items.map((item, i) => (
          <Link
            key={item.title}
            href={ITEM_HREFS[i]}
            className="mk-menu-item"
            style={{
              display: "flex",
              gap: 13,
              alignItems: "flex-start",
              padding: "13px 14px",
              borderRadius: 13,
              color: "var(--ink)",
            }}
          >
            <span
              aria-hidden="true"
              style={{
                width: 32,
                height: 32,
                borderRadius: 10,
                background: ITEM_TINTS[i],
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 14,
                flex: "none",
              }}
            >
              {ITEM_ICONS[i]}
            </span>
            <span style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <span style={{ fontSize: 14, fontWeight: 700 }}>{item.title}</span>
              <span style={{ fontSize: 12.5, fontWeight: 500, color: "var(--ink-3)" }}>
                {item.blurb}
              </span>
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}

export function MarketingNav() {
  const { user } = useAuth();
  return (
    <header
      style={{
        position: "sticky",
        top: 0,
        zIndex: 40,
        background: "rgba(247,237,224,0.86)",
        backdropFilter: "blur(14px)",
        WebkitBackdropFilter: "blur(14px)",
        borderBottom: "1px solid rgba(232,218,203,0.8)",
      }}
    >
      <div
        style={{
          maxWidth: 1140,
          margin: "0 auto",
          padding: "0 28px",
          height: 66,
          display: "flex",
          alignItems: "center",
          gap: 26,
        }}
      >
        <Link
          href={LINKS.top}
          style={{ display: "flex", alignItems: "center", gap: 9, color: "var(--ink)" }}
        >
          <span
            aria-hidden="true"
            style={{
              width: 30,
              height: 30,
              borderRadius: 10,
              background: "var(--terracotta)",
              color: "#fff9f2",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 800,
              fontSize: 15,
            }}
          >
            {copy.nav.brand.charAt(0)}
          </span>
          <span style={{ fontSize: 17, fontWeight: 800, letterSpacing: "-0.01em" }}>
            {copy.nav.brand}
          </span>
        </Link>
        <div style={{ flex: 1 }} />
        <nav
          aria-label={copy.nav.ariaLabel}
          style={{ display: "flex", alignItems: "center", gap: 22 }}
        >
          <FeaturesMenu />
          <Link href={LINKS.how} className="mk-link mk-desktop-only" style={TEXT_LINK}>
            {copy.nav.howItWorks}
          </Link>
          <Link href={LINKS.security} className="mk-link mk-desktop-only" style={TEXT_LINK}>
            {copy.nav.security}
          </Link>
          {user ? (
            <Link href={LINKS.app} className="mk-cta" style={CTA}>
              {copy.nav.openApp}
            </Link>
          ) : (
            <>
              <Link href={LINKS.login} className="mk-link" style={TEXT_LINK}>
                {copy.nav.signIn}
              </Link>
              <Link href={LINKS.signup.nav} className="mk-cta" style={CTA}>
                {copy.nav.cta}
              </Link>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}
