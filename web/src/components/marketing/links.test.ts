import { describe, expect, it } from "vitest";
import { LINKS } from "@/components/marketing/links";
import { signupFrom } from "@/lib/nav";

describe("marketing links", () => {
  it("tags every signup CTA with a source /signup will accept", () => {
    const sources = Object.values(LINKS.signup).map((href) => {
      const from = signupFrom(new URL(href, "http://h").searchParams.get("from"));
      expect(from, href).not.toBeNull();
      return from;
    });
    expect(new Set(sources)).toEqual(new Set(["hero", "closing", "nav", "footer"]));
  });

  it("writes in-page anchors as /#x so they work from the placeholder pages", () => {
    for (const href of [LINKS.top, LINKS.how, LINKS.journeys, LINKS.security, LINKS.start]) {
      expect(href.startsWith("/#"), href).toBe(true);
    }
  });
});
