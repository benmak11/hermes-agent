import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { JourneyTrack } from "@/components/warm/JourneyTrack";
import { deriveTrack } from "@/components/warm/journeyStages";
import { HERO_STAGES } from "@/components/marketing/heroDemo";

/**
 * The marketing hero's rendered track, captured from the tree at `26df188`
 * — BEFORE the `ended` prop existed (`npx vitest run` on a throwaway spec
 * that wrote `renderToStaticMarkup(<JourneyTrack stages={[...HERO_STAGES]}
 * layout="fluid" />)` to a file, then deleted). `JourneyTrack` is the
 * marketing↔app seam: any app-side change to it has to leave this byte-identical.
 */
const HERO_MARKUP_BEFORE_ENDED =
  `<div role="list" style="display:flex;align-items:flex-start"><div role="listitem" style="flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:8px"><span aria-hidden="true" style="width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;box-sizing:border-box;flex:none;background:var(--sage);color:#f6faf3;font-size:13px;font-weight:800">✓</span><span style="font-size:12.5px;text-align:center;font-weight:600;color:var(--ink-2)">Applied</span><span style="font-size:11.5px;color:var(--ink-4)">2 Sep</span></div><div aria-hidden="true" style="flex:1;height:2px;margin-top:13px;background:var(--sage)"></div><div role="listitem" style="flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:8px"><span aria-hidden="true" style="width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;box-sizing:border-box;flex:none;background:var(--sage);color:#f6faf3;font-size:13px;font-weight:800">✓</span><span style="font-size:12.5px;text-align:center;font-weight:600;color:var(--ink-2)">Recruiter call</span><span style="font-size:11.5px;color:var(--sage)">went well</span></div><div aria-hidden="true" style="flex:1;height:2px;margin-top:13px;background:var(--sage)"></div><div role="listitem" style="flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:8px"><span aria-current="step" style="width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;box-sizing:border-box;flex:none;background:var(--terracotta-tint);border:2px solid var(--terracotta);animation:jpulse 2.6s ease-in-out infinite"><span style="width:9px;height:9px;border-radius:50%;background:var(--terracotta)"></span></span><span style="font-size:12.5px;text-align:center;font-weight:800;color:var(--terracotta)">System design</span><span style="font-size:11.5px;color:var(--terracotta)">Fri · you&#x27;re here</span></div><div aria-hidden="true" style="flex:1;height:2px;margin-top:13px;background:var(--border-warm-hair)"></div><div role="listitem" style="flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:8px"><span aria-hidden="true" style="width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;box-sizing:border-box;flex:none;background:var(--surface-warm);border:1px dashed #d9c4a8"></span><span style="font-size:12.5px;text-align:center;font-weight:600;color:var(--ink-4)">Hiring manager</span><span style="font-size:11.5px;color:var(--ink-4)">not booked</span></div><div aria-hidden="true" style="flex:1;height:2px;margin-top:13px;background:#f4ebdf"></div><div role="listitem" style="flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:8px"><span aria-hidden="true" style="width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;box-sizing:border-box;flex:none;background:var(--surface-warm);border:1px dashed #d9c4a8"></span><span style="font-size:12.5px;text-align:center;font-weight:600;color:var(--ink-4)">Decision</span><span style="font-size:11.5px;color:var(--ink-4)">—</span></div></div>`;

// Pull the inline style attribute of every connector, in document order.
function connectors(html: string): string[] {
  return [...html.matchAll(/<div aria-hidden="true" style="([^"]*)"/g)].map(
    (m) => m[1],
  );
}

describe("JourneyTrack", () => {
  const stages = deriveTrack(["Applied", "Recruiter", "Onsite", "Offer"], 2);
  const html = renderToStaticMarkup(<JourneyTrack stages={stages} />);

  it("marks exactly one node as the current step and pulses it", () => {
    const current = [...html.matchAll(/<span aria-current="step" style="([^"]*)"/g)];
    expect(current).toHaveLength(1);
    expect(current[0][1]).toContain("jpulse");
  });

  it("renders a check in every done node", () => {
    const checks = html.match(/✓/g) ?? [];
    expect(checks).toHaveLength(2);
    expect(html).toContain("background:var(--sage)");
  });

  it("colours each connector by the node it leaves from", () => {
    const styles = connectors(html);
    expect(styles).toHaveLength(stages.length - 1);
    // after done → sage; after the current node → hairline; there is no
    // connector after the last node.
    expect(styles[0]).toContain("background:var(--sage)");
    expect(styles[1]).toContain("background:var(--sage)");
    expect(styles[2]).toContain("background:var(--border-warm-hair)");
  });

  it("uses #f4ebdf after an upcoming node", () => {
    const early = renderToStaticMarkup(
      <JourneyTrack stages={deriveTrack(["a", "b", "c"], 0)} />,
    );
    const styles = connectors(early);
    expect(styles[0]).toContain("background:var(--border-warm-hair)");
    expect(styles[1]).toContain("background:#f4ebdf");
  });

  it("lays out list semantics and the fluid column", () => {
    expect(html).toContain('role="list"');
    expect(html.match(/role="listitem"/g)).toHaveLength(stages.length);
    expect(html).toContain("flex:1;min-width:0");
    expect(html).not.toContain("width:112px");
  });

  it("emits fixed 112px columns in fixed layout", () => {
    const fixed = renderToStaticMarkup(
      <JourneyTrack stages={stages} layout="fixed" />,
    );
    expect(fixed).toContain("width:112px");
    expect(fixed).toContain("flex:none");
  });

  it("renders the optional note with its tone", () => {
    const noted = renderToStaticMarkup(
      <JourneyTrack
        stages={[
          { id: "0", name: "Recruiter", status: "done", note: "went well", noteTone: "good" },
          { id: "1", name: "Onsite", status: "current", note: "Fri", noteTone: "accent" },
          { id: "2", name: "Offer", status: "upcoming", note: "—" },
        ]}
      />,
    );
    expect(noted).toContain("color:var(--sage)\">went well");
    expect(noted).toContain("color:var(--terracotta)\">Fri");
    expect(noted).toContain("color:var(--ink-4)\">—");
  });
  // PR11 M12 — the seam. `ended` is optional, so the hero must be untouched.
  it("leaves the marketing hero's markup byte-identical", () => {
    const hero = renderToStaticMarkup(
      <JourneyTrack stages={[...HERO_STAGES]} layout="fluid" />,
    );
    expect(hero).toBe(HERO_MARKUP_BEFORE_ENDED);
    // No node is a link: the check-in is reached from the board, the stage
    // page and the week strip, never from inside this component.
    expect(hero).not.toContain("<a ");
    expect(hero).not.toContain("href");
  });

  // PR11 M13
  it("renders a brick ✕ for an ended stage", () => {
    const ended = renderToStaticMarkup(
      <JourneyTrack
        stages={[{ id: "0", name: "Technical", status: "done", ended: true }]}
      />,
    );
    expect(ended).toContain("✕");
    expect(ended).toContain("background:var(--brick)");
    expect(ended).toContain("color:var(--brick)");

    const plain = renderToStaticMarkup(
      <JourneyTrack stages={[{ id: "0", name: "Technical", status: "done" }]} />,
    );
    expect(plain).toContain("✓");
    expect(plain).toContain("background:var(--sage)");
    expect(plain).not.toContain("✕");
  });
});
