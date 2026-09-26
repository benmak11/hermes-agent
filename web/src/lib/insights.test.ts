import { describe, expect, it } from "vitest";

import {
  MIN_SAMPLE,
  barTone,
  carryForward,
  countLine,
  endStageRows,
  enoughToCall,
  insightsHeader,
  loopShapes,
  matchNote,
  sampleNote,
  topTag,
} from "@/lib/insights";
import type { CheckIn, Journey, JourneyStage, PrepItem } from "@/lib/types";

const UTC = { timeZone: "UTC" };

function stage(over: Partial<JourneyStage> & Pick<JourneyStage, "id">): JourneyStage {
  return {
    name: over.id,
    status: "upcoming",
    scheduled_at: null,
    format: null,
    who: null,
    checkin: null,
    questions: [],
    prep: [],
    ...over,
  };
}

function checkin(sentence: string | null, tags: string[] = []): CheckIn {
  return { rating: null, tags, sentence, at: "2026-09-01T00:00:00Z" };
}

function j(over: Partial<Journey> = {}): Journey {
  return {
    id: "j1",
    user_id: "u1",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:00Z",
    company: "Shopify",
    role: "Staff Engineer",
    source: "manual",
    application_id: null,
    job_url: null,
    stages: [],
    outcome: "in_progress",
    ended_at_stage_id: null,
    retro: null,
    ...over,
  };
}

const applied = () => stage({ id: "applied", name: "Applied", status: "done" });
const prep = (text: string): PrepItem => ({ text, done: false, from_journey_id: null });
const row = (rows: ReturnType<typeof endStageRows>, name: string) =>
  rows.find((r) => r.name === name);

describe("endStageRows", () => {
  // M1 — the one that matters: an in-flight stage is neither passed nor failed.
  it("never counts a live journey's current stage", () => {
    const rows = endStageRows([
      j({
        stages: [
          applied(),
          stage({ id: "rec", name: "Recruiter call", status: "done" }),
          stage({ id: "tech", name: "System design", status: "current" }),
          stage({ id: "dec", name: "Decision", status: "upcoming" }),
        ],
      }),
    ]);
    expect(rows.map((r) => r.name)).toEqual(["Recruiter call"]);
    expect(row(rows, "Recruiter call")).toMatchObject({ reached: 1, gotPast: 1 });
    expect(row(rows, "System design")).toBeUndefined();
  });

  // M2 — the ended stage is reached but not passed; the same stage on an
  // offer journey did get past.
  it("counts the ended stage as reached-not-past, but only on a closed journey", () => {
    const stages = () => [
      applied(),
      stage({ id: "tech", name: "Technical screen", status: "done" }),
    ];
    const rejected = endStageRows([
      j({ stages: stages(), outcome: "rejected", ended_at_stage_id: "tech" }),
    ]);
    expect(row(rejected, "Technical screen")).toMatchObject({ reached: 1, gotPast: 0 });

    const offered = endStageRows([
      j({ stages: stages(), outcome: "offer", ended_at_stage_id: null }),
    ]);
    expect(row(offered, "Technical screen")).toMatchObject({ reached: 1, gotPast: 1 });
  });

  // M3 — grouping, spelling, and the Applied exclusion.
  it("groups case-insensitively, prefers the commonest spelling, drops Applied", () => {
    const one = (id: string, name: string) =>
      j({ id, stages: [applied(), stage({ id: "t", name, status: "done" })] });
    const rows = endStageRows([
      one("a", "Technical screen"),
      one("b", "technical screen"),
      one("c", "Technical screen"),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].name).toBe("Technical screen");
    expect(rows[0].reached).toBe(3);
    expect(rows.some((r) => r.key === "applied")).toBe(false);
  });

  // M4 — loop order, not alphabetical order.
  it("orders rows by mean index within the loop", () => {
    const one = (id: string) =>
      j({
        id,
        outcome: "offer",
        stages: [
          applied(),
          stage({ id: "r", name: "Recruiter", status: "done" }),
          stage({ id: "h", name: "Hiring manager", status: "done" }),
          stage({ id: "o", name: "Onsite", status: "done" }),
        ],
      });
    const rows = endStageRows([one("a"), one("b")]);
    expect(rows.map((r) => r.name)).toEqual(["Recruiter", "Hiring manager", "Onsite"]);
  });
});

describe("barTone", () => {
  const r = (reached: number, gotPast: number) => ({
    key: "k",
    name: "n",
    reached,
    gotPast,
    order: 0,
  });

  // M5 — no verdict below MIN_SAMPLE, however clean the ratio looks.
  it("refuses a verdict below MIN_SAMPLE", () => {
    expect(MIN_SAMPLE).toBe(3);
    expect(barTone(r(2, 2))).toBe("unknown");
    expect(barTone(r(1, 0))).toBe("unknown");
  });
  it("calls it once there is enough", () => {
    expect(barTone(r(3, 3))).toBe("good");
    expect(barTone(r(5, 2))).toBe("warn");
    expect(barTone(r(3, 1))).toBe("bad");
  });
});

// M16
describe("enoughToCall", () => {
  it("is inclusive at MIN_SAMPLE", () => {
    expect(enoughToCall(2)).toBe(false);
    expect(enoughToCall(3)).toBe(true);
  });
});

describe("topTag", () => {
  const withTags = (id: string, ...tags: string[][]) =>
    j({
      id,
      stages: tags.map((t, i) => stage({ id: `s${i}`, checkin: checkin(null, t) })),
    });

  // M6
  it("needs a repeat, and counts the denominator over every check-in", () => {
    const got = topTag([
      withTags("a", ["Ran out of time"], ["Good rapport"]),
      withTags("b", ["Ran out of time"], []),
    ]);
    expect(got).toEqual({ tag: "Ran out of time", count: 2, ofCheckIns: 4 });
  });
  it("returns null when nothing repeats, and when there are no check-ins", () => {
    expect(topTag([withTags("a", ["One"], ["Two"])])).toBeNull();
    expect(topTag([j({ stages: [applied()] })])).toBeNull();
    expect(topTag([])).toBeNull();
  });
});

describe("carryForward", () => {
  // M7
  it("dedupes across journeys and merges the attribution", () => {
    const text = "Spend the first five minutes on requirements";
    const items = carryForward([
      j({ id: "a", company: "Notion", retro: { keep: null, change: null, next: text } }),
      j({ id: "b", company: "Stripe", retro: { keep: null, change: null, next: text } }),
    ]);
    expect(items).toHaveLength(1);
    expect(items[0].text).toBe(text);
    expect(items[0].journeyIds).toHaveLength(2);
    expect(items[0].note).toBe("You wrote this after Notion and Stripe.");
  });

  // Fix 3: the company list is what gets named, so it is deduped.
  it("never names the same employer twice", () => {
    const text = "Ask about on-call before the offer stage";
    const one = (id: string, updated: string) =>
      j({
        id,
        company: "Stripe",
        updated_at: updated,
        retro: { keep: null, change: null, next: text },
      });
    const items = carryForward([one("a", "2026-09-01T00:00:00Z"), one("b", "2026-09-09T00:00:00Z")]);
    expect(items).toHaveLength(1);
    expect(items[0].journeyIds).toHaveLength(2);
    expect(items[0].note).not.toContain("Stripe and Stripe");
    expect(items[0].note).toBe("From your Stripe retro.");
  });

  it("names the count above two journeys", () => {
    const text = "Ask what the first 90 days look like";
    const one = (id: string, company: string) =>
      j({ id, company, retro: { keep: null, change: null, next: text } });
    const items = carryForward([
      one("a", "Notion"),
      one("b", "Stripe"),
      one("c", "Linear"),
    ]);
    expect(items[0].note).toBe("You wrote this after Notion, Stripe and 1 more.");
  });

  // M8 — the legacy format, read exactly.
  it("takes only the 'To improve' half of a legacy sentence, and raw ones whole", () => {
    const one = (id: string, sentence: string) =>
      j({ id, stages: [stage({ id: "s", name: "Onsite", checkin: checkin(sentence) })] });
    const texts = (js: Journey[]) => carryForward(js, 10).map((i) => i.text);

    expect(texts([one("a", "Went well: rapport · To improve: ask more back")])).toEqual([
      "ask more back",
    ]);
    expect(texts([one("b", "Went well: rapport")])).toEqual([]);
    expect(texts([one("c", "Spend five minutes on requirements")])).toEqual([
      "Spend five minutes on requirements",
    ]);
  });

  // M9 — the prep-list note, case-insensitive, live journeys only.
  it("spots an existing prep item on a live journey only", () => {
    const text = "Two timed concurrency problems a week";
    const source = j({
      id: "a",
      company: "Notion",
      updated_at: "2026-09-10T00:00:00Z",
      outcome: "rejected",
      ended_at_stage_id: null,
      retro: { keep: null, change: null, next: text },
    });
    const holder = (outcome: Journey["outcome"]) =>
      j({
        id: "b",
        company: "Shopify",
        updated_at: "2026-09-02T00:00:00Z",
        outcome,
        stages: [stage({ id: "s", name: "Onsite", prep: [prep("two TIMED concurrency problems a week")] })],
      });

    expect(carryForward([source, holder("in_progress")])[0].note).toBe(
      "Already on your Shopify prep list.",
    );
    expect(carryForward([source, holder("rejected")])[0].note).toBe("From your Notion retro.");
  });

  it("falls back to the check-in's own stage and company", () => {
    const items = carryForward([
      j({
        id: "a",
        company: "Linear",
        stages: [stage({ id: "s", name: "System design", checkin: checkin("Draw the boxes first") })],
      }),
    ]);
    expect(items[0].note).toBe("From your System design check-in at Linear.");
  });

  // M10 — repeats first, then recency; default limit of 3.
  it("orders by repeats then recency and honours the limit", () => {
    const repeated = "Lead with the ledger rewrite";
    const list = [
      j({
        id: "a",
        company: "Notion",
        updated_at: "2026-09-01T00:00:00Z",
        retro: { keep: null, change: "Oldest note", next: repeated },
      }),
      j({
        id: "b",
        company: "Stripe",
        updated_at: "2026-09-20T00:00:00Z",
        retro: { keep: null, change: "Newest note", next: repeated },
      }),
      j({
        id: "c",
        company: "Linear",
        updated_at: "2026-09-10T00:00:00Z",
        retro: { keep: null, change: null, next: "Middle note" },
      }),
    ];
    expect(carryForward(list, 10).map((i) => i.text)).toEqual([
      repeated,
      "Newest note",
      "Middle note",
      "Oldest note",
    ]);
    expect(carryForward(list)).toHaveLength(3);
  });

  it("is empty when nothing was written", () => {
    expect(carryForward([j({ stages: [applied()] })])).toEqual([]);
  });
});

describe("loopShapes", () => {
  const loop = (id: string, names: string[], over: Partial<Journey> = {}) =>
    j({ id, stages: names.map((n, i) => stage({ id: `${id}-${i}`, name: n })), ...over });

  // M11 — one journey, one row, best match wins.
  it("assigns each journey to exactly one template", () => {
    const journeys = [
      loop("a", ["Recruiter call", "Hiring manager", "System design", "Decision"]),
      loop("b", ["Take-home", "Technical review", "Hiring manager"]),
    ];
    const rows = loopShapes(journeys);
    expect(rows.find((r) => r.label === "Staff / leadership loop")?.journeys).toBe(1);
    expect(rows.find((r) => r.label === "Take-home first")?.journeys).toBe(1);
    expect(rows.reduce((n, r) => n + r.journeys, 0)).toBe(2);
    expect(rows.find((r) => r.label === "Standard engineering loop")).toBeUndefined();
  });

  // M12 — two names is the floor, and `scratch` never appears.
  it("requires two matched names and skips the scratch template", () => {
    expect(loopShapes([loop("a", ["Recruiter call", "Coffee chat"])])).toEqual([]);
    const rows = loopShapes([loop("b", ["Recruiter call", "Technical screen"])]);
    expect(rows.every((r) => r.id !== "scratch")).toBe(true);
    expect(rows).toHaveLength(1);
  });

  // M13 — counts, never a rate.
  it("counts outcomes without ever computing one", () => {
    const names = ["Take-home", "Technical review", "Hiring manager"];
    const rows = loopShapes([
      loop("a", names, { outcome: "offer" }),
      loop("b", names, { outcome: "rejected" }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ journeys: 2, offers: 1, closed: 1, live: 0 });

    const mixed = loopShapes([
      loop("c", names, { outcome: "withdrawn" }),
      loop("d", names, { outcome: "in_progress" }),
    ]);
    expect(mixed[0]).toMatchObject({ journeys: 2, offers: 0, closed: 1, live: 1 });
  });
});

describe("matchNote", () => {
  // The rows only cover journeys whose stage names look like a template, so
  // these fixtures go through the real LOOP_TEMPLATES and the real loopShapes:
  // the arithmetic is pinned from journeys all the way to the sentence, not
  // from a hand-built row shape that could drift from what loopShapes emits.
  const loop = (id: string, names: string[]) =>
    j({ id, stages: names.map((n, i) => stage({ id: `${id}-${i}`, name: n })) });
  const STANDARD = ["Recruiter call", "Technical screen", "Onsite loop", "Decision"];
  const TAKEHOME = ["Take-home", "Technical review", "Hiring manager"];
  const NEITHER = ["Coffee chat", "Portfolio walkthrough"];

  // MN1 — the reconciliation the page's header depends on.
  it("reconciles with the journey count the header prints", () => {
    const journeys = [
      loop("a", STANDARD),
      loop("b", STANDARD),
      loop("c", TAKEHOME),
      loop("d", NEITHER),
      loop("e", NEITHER),
      loop("f", NEITHER),
    ];
    const shapes = loopShapes(journeys);
    const matched = shapes.reduce((n, r) => n + r.journeys, 0);
    expect(matched).toBe(3);
    expect(matched + 3).toBe(journeys.length);
    expect(matchNote(journeys.length, shapes)).toBe(
      "Matched by stage name — 3 journeys don't look like any of these shapes.",
    );
  });

  // MN2 — the all-matched branch never says "0 journeys don't".
  it("says so plainly when every journey matched", () => {
    const journeys = [loop("a", STANDARD), loop("b", TAKEHOME)];
    const shapes = loopShapes(journeys);
    expect(shapes.reduce((n, r) => n + r.journeys, 0)).toBe(journeys.length);
    expect(matchNote(journeys.length, shapes)).toBe(
      "Matched by stage name across all 2 of your journeys.",
    );
  });

  // MN3 — the singular branch.
  it("is singular for one unmatched journey", () => {
    const journeys = [loop("a", STANDARD), loop("b", TAKEHOME), loop("c", NEITHER)];
    expect(matchNote(journeys.length, loopShapes(journeys))).toBe(
      "Matched by stage name — 1 journey doesn't look like any of these shapes.",
    );
  });

  it("counts every journey as unmatched when nothing matched", () => {
    const journeys = [loop("a", NEITHER), loop("b", NEITHER)];
    expect(loopShapes(journeys)).toEqual([]);
    expect(matchNote(journeys.length, [])).toBe(
      "Matched by stage name — 2 journeys don't look like any of these shapes.",
    );
  });
});

describe("countLine", () => {
  const r = (over: Partial<ReturnType<typeof loopShapes>[number]>) => ({
    id: "takehome",
    label: "Take-home first",
    journeys: 0,
    offers: 0,
    closed: 0,
    live: 0,
    ...over,
  });

  // MN4/MN5 — the plural edges.
  it("pluralises journeys and offers independently", () => {
    expect(countLine(r({ journeys: 1, offers: 1, closed: 0 }))).toBe("1 journey — 1 offer, 0 closed");
    expect(countLine(r({ journeys: 2, offers: 0, closed: 2 }))).toBe("2 journeys — 0 offers, 2 closed");
    expect(countLine(r({ journeys: 3, offers: 2, closed: 1 }))).toBe("3 journeys — 2 offers, 1 closed");
  });

  // MN6 — an unfinished journey is named, not folded into a column.
  it("names the journeys still going, and only when there are some", () => {
    expect(countLine(r({ journeys: 3, offers: 1, closed: 1, live: 1 }))).toBe(
      "3 journeys — 1 offer, 1 closed, 1 still going",
    );
    expect(countLine(r({ journeys: 2, offers: 1, closed: 1, live: 0 }))).not.toContain(
      "still going",
    );
  });

  it("never prints a ratio", () => {
    const line = countLine(r({ journeys: 2, offers: 2, closed: 0 }));
    expect(line).not.toMatch(/ of |%/);
  });
});

// M14
describe("insightsHeader", () => {
  it("counts check-ins, not stages, and dates from the oldest journey", () => {
    const got = insightsHeader(
      [
        j({
          id: "a",
          created_at: "2026-03-14T00:00:00Z",
          stages: [
            applied(),
            stage({ id: "s1", checkin: checkin("one") }),
            stage({ id: "s2", checkin: checkin("two") }),
          ],
        }),
        j({
          id: "b",
          created_at: "2026-06-02T00:00:00Z",
          stages: [stage({ id: "s3", checkin: checkin("three") })],
        }),
        j({
          id: "c",
          created_at: "2026-08-20T00:00:00Z",
          stages: [
            stage({ id: "s4", checkin: checkin("four") }),
            stage({ id: "s5", checkin: checkin("five") }),
            stage({ id: "s6" }),
          ],
        }),
      ],
      UTC,
    );
    expect(got).toEqual({ journeys: 3, checkIns: 5, since: "14 Mar" });
  });

  it("has nothing to date when there are no journeys", () => {
    expect(insightsHeader([], UTC)).toEqual({ journeys: 0, checkIns: 0, since: null });
  });
});

// M15
describe("sampleNote", () => {
  it("says a small sample is a small sample, singular and plural", () => {
    expect(sampleNote(1, 2)).toContain("1 journey is a small sample");
    expect(sampleNote(1, 2)).toContain("hints, not verdicts");
    expect(sampleNote(5, 14)).toContain("5 journeys are a small sample");
  });
  it("switches to a plain count at ten", () => {
    const note = sampleNote(12, 40);
    expect(note).toContain("Counted from your 12 journeys and 40 check-ins");
    expect(note).not.toContain("small sample");
  });
});
