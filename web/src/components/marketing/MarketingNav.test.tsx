import { describe, expect, it } from "vitest";
import type { User } from "firebase/auth";
import { renderToStaticMarkup } from "react-dom/server";
import { MarketingNav } from "@/components/marketing/MarketingNav";
import { copy } from "@/components/marketing/copy";
import { AuthContext, type AuthState } from "@/lib/authContext";

function render(value: AuthState): string {
  return renderToStaticMarkup(
    <AuthContext.Provider value={value}>
      <MarketingNav />
    </AuthContext.Provider>,
  );
}

describe("MarketingNav", () => {
  it("shows Sign in + Request an invite when signed out", () => {
    const html = render({ user: null, loading: false });
    expect(html).toContain('href="/login"');
    expect(html).toContain('href="/signup?from=nav"');
    expect(html).toContain(copy.nav.cta);
    expect(html).not.toContain(copy.nav.openApp);
  });

  it("renders the same signed-out CTAs in the SSR state (loading)", () => {
    // The static HTML must always carry the CTAs; `loading` is ignored.
    expect(render({ user: null, loading: true })).toBe(
      render({ user: null, loading: false }),
    );
  });

  it("swaps both for Open Hermes → when signed in", () => {
    const html = render({ user: { uid: "u" } as unknown as User, loading: false });
    expect(html).toContain('href="/app"');
    expect(html).toContain(copy.nav.openApp);
    expect(html).not.toContain("/signup");
    expect(html).not.toContain('href="/login"');
  });

  it("renders the Features disclosure closed, with the panel inert", () => {
    const html = render({ user: null, loading: false });
    const trigger = html.match(/<button[^>]*>/)?.[0] ?? "";
    expect(trigger).toContain('aria-expanded="false"');
    const controls = trigger.match(/aria-controls="([^"]+)"/)?.[1];
    expect(controls).toBeTruthy();
    const panel = html.match(new RegExp(`<div id="${controls}"[^>]*>`))?.[0] ?? "";
    expect(panel).toContain('inert=""');
  });
});
