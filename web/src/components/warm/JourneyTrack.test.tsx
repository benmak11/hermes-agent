import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { JourneyTrack } from "@/components/warm/JourneyTrack";
import { deriveTrack } from "@/components/warm/journeyStages";

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
});
