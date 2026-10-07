import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { liveSampled, MinScoreControl, sampledNote } from "./MinScoreControl";

/**
 * While the exploration sample is on, the review queue returns a few jobs
 * under the slider's value. The control has to say so — and only the control:
 * a card that looks different tells the user its decision does not count.
 */

const noop = () => {};

const render = (sampled: number | undefined, minScore = 60) =>
  renderToStaticMarkup(
    <MinScoreControl minScore={minScore} sampled={sampled} onChange={noop} />,
  );

/** Markup with tags stripped, so assertions read like the screen does. */
const text = (html: string) => html.replace(/<[^>]+>/g, "").replace(/\s+/g, " ");

describe("Min score control", () => {
  it("reads exactly as before when the response has no sampled count", () => {
    expect(text(render(undefined))).toBe("Min score60");
  });

  it("owns up to the sampled jobs when there are some", () => {
    expect(text(render(2))).toBe("Min score60· plus 2 lower matches, sampled");
  });

  it("uses the singular for one", () => {
    expect(text(render(1, 75))).toBe("Min score75· plus 1 lower match, sampled");
  });

  it("says nothing at zero", () => {
    expect(text(render(0))).toBe("Min score60");
  });

  it("sampledNote is null unless something was sampled", () => {
    expect(sampledNote(undefined)).toBeNull();
    expect(sampledNote(0)).toBeNull();
    expect(sampledNote(3)).toBe("plus 3 lower matches, sampled");
  });
});

describe("liveSampled", () => {
  it("stays absent when the server sent no count", () => {
    expect(liveSampled(undefined, [35, 90], 60)).toBeUndefined();
  });

  it("drops as under-floor cards are decided off the cached list", () => {
    expect(liveSampled(2, [90, 45, 35], 60)).toBe(2);
    expect(liveSampled(2, [90, 35], 60)).toBe(1);
    expect(liveSampled(2, [90], 60)).toBe(0);
  });

  it("never exceeds the server's count", () => {
    expect(liveSampled(1, [45, 35], 60)).toBe(1);
  });
});

describe("/app marks no individual card", () => {
  const SRC = readFileSync(
    fileURLToPath(new URL("./page.tsx", import.meta.url)),
    "utf8",
  );

  it("renders the slider through MinScoreControl, fed the server's count", () => {
    expect(SRC).toContain("<MinScoreControl");
    expect(SRC).toMatch(/liveSampled\(\s*data\?\.sampled,/);
  });

  it("uses the count nowhere but the control", () => {
    const code = SRC.replace(/\/\/[^\n]*/g, "").replace(/\/\*[\s\S]*?\*\//g, "");
    // Every line that touches the count: the import, the response type's
    // field, and the one prop that feeds the control. A card reading it would
    // add a line here.
    const lines = code
      .split("\n")
      .filter((l) => /sampled/i.test(l))
      .map((l) => l.trim());
    expect(lines).toEqual([
      'import { liveSampled, MinScoreControl } from "./MinScoreControl";',
      "sampled?: number;",
      "sampled={liveSampled(",
      "data?.sampled,",
    ]);
    expect(code.toLowerCase()).not.toContain("exploration");
  });
});
