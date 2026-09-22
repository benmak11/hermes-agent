import { describe, expect, it } from "vitest";

import {
  LOOP_TEMPLATES,
  buildStagesFromTemplate,
  draftToStages,
  emptyStage,
  fromLocalInput,
  hasData,
  loopUnknown,
  normalizeStatuses,
  relativeWhen,
  seedPrep,
  toLocalInput,
} from "@/lib/journeyEdit";
import type { CheckIn, Journey, JourneyStage } from "@/lib/types";

const UTC = { timeZone: "UTC" };
const now = new Date("2026-09-21T12:00:00Z");

/** Deterministic ids: the whole point of the injectable `newId`. */
function ids() {
  let n = 0;
  return () => `n${++n}`;
}

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

function checkin(sentence: string): CheckIn {
  return { rating: null, tags: [], sentence, at: "2026-09-01T00:00:00Z" };
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
const names = (s: JourneyStage[]) => s.map((x) => x.name);
const statuses = (s: JourneyStage[]) => s.map((x) => x.status);

describe("LOOP_TEMPLATES", () => {
  it("has the four the design shows, with its counts", () => {
    expect(LOOP_TEMPLATES.map((t) => [t.id, t.stages.length])).toEqual([
      ["engineering", 4],
      ["takehome", 3],
      ["staff", 5],
      ["scratch", 0],
    ]);
  });
});

describe("hasData", () => {
  it("is true for a done stage, a check-in, prep or questions; false otherwise", () => {
    expect(hasData(stage({ id: "a" }))).toBe(false);
    expect(hasData(stage({ id: "a", status: "done" }))).toBe(true);
    expect(hasData(stage({ id: "a", checkin: checkin("x") }))).toBe(true);
    expect(hasData(stage({ id: "a", prep: [{ text: "p", done: false }] }))).toBe(true);
    expect(hasData(stage({ id: "a", questions: [{ text: "q" }] }))).toBe(true);
  });
});

describe("buildStagesFromTemplate", () => {
  // M1
  it("a matched template name reuses the stored stage", () => {
    const note = checkin("To improve: drill concurrency");
    const existing = [
      applied(),
      stage({ id: "rec", name: "Recruiter call", status: "done", checkin: note }),
    ];
    const out = buildStagesFromTemplate(
      ["Recruiter call", "Technical screen"],
      existing,
      ids(),
    );
    expect(names(out)).toEqual(["Applied", "Recruiter call", "Technical screen"]);
    const rec = out[1];
    expect(rec.id).toBe("rec");
    expect(rec.checkin).toEqual(note);
    expect(out[2].id).toBe("n1");
  });

  // M2
  it("an unmatched stage with data is carried over; an empty one is dropped", () => {
    const phone = stage({ id: "ph", name: "Phone" });
    expect(names(buildStagesFromTemplate(["Onsite"], [applied(), phone], ids()))).toEqual([
      "Applied",
      "Onsite",
    ]);
    // An unfinished carried stage trails the template block (see the ordering
    // test below for why finished ones must lead instead).
    const withNote = { ...phone, checkin: checkin("went fine") };
    expect(names(buildStagesFromTemplate(["Onsite"], [applied(), withNote], ids()))).toEqual([
      "Applied",
      "Onsite",
      "Phone",
    ]);
  });

  // M2b — a finished carried stage must lead the template block, or
  // normalizeStatuses demotes it back to `upcoming` and it loses its ✓.
  it("a carried DONE stage keeps its done status when a template is applied", () => {
    const rec = {
      ...stage({ id: "rec", name: "Recruiter call", status: "done" }),
      checkin: checkin("went well"),
    };
    const onsite = stage({ id: "on", name: "Onsite", prep: [{ text: "read the docs", done: false }] });
    const out = buildStagesFromTemplate(
      ["Hiring manager", "System design"],
      [applied(), rec, onsite],
      ids(),
    );
    const byName = Object.fromEntries(out.map((x) => [x.name, x]));
    expect(byName["Recruiter call"].status).toBe("done");
    expect(byName["Recruiter call"].checkin).not.toBeNull();
    expect(names(out)).toEqual([
      "Applied",
      "Recruiter call",
      "Hiring manager",
      "System design",
      "Onsite",
    ]);
    expect(out.filter((x) => x.status === "current")).toHaveLength(1);
  });

  // M3
  it("leaves exactly one current, never two", () => {
    const existing = [applied(), stage({ id: "rec", name: "Recruiter call", status: "done" })];
    const out = buildStagesFromTemplate(["A", "B", "C"], existing, ids());
    expect(statuses(out)).toEqual(["done", "done", "current", "upcoming", "upcoming"]);
    expect(out.filter((s) => s.status === "current")).toHaveLength(1);

    const allDone = normalizeStatuses([applied(), stage({ id: "b", status: "done" })]);
    expect(statuses(allDone)).toEqual(["done", "done"]);
    expect(allDone.filter((s) => s.status === "current")).toHaveLength(0);

    for (const t of LOOP_TEMPLATES) {
      const built = buildStagesFromTemplate(t.stages, existing, ids());
      expect(built.filter((s) => s.status === "current").length).toBeLessThanOrEqual(1);
    }
  });

  // M4
  it("gives new stages fresh unique ids", () => {
    const out = buildStagesFromTemplate(["A", "B", "C"], [applied()], ids());
    const all = out.map((s) => s.id);
    expect(new Set(all).size).toBe(all.length);
    expect(all).toEqual(["applied", "n1", "n2", "n3"]);
  });

  it("emptyStage uses the injected id and starts upcoming and empty", () => {
    const s = emptyStage("Onsite", ids());
    expect(s).toEqual({
      id: "n1",
      name: "Onsite",
      status: "upcoming",
      scheduled_at: null,
      format: null,
      who: null,
      checkin: null,
      questions: [],
      prep: [],
    });
  });
});

describe("draftToStages", () => {
  // M5
  it("never drops a stage with a check-in, and drops blank-named rows", () => {
    const note = checkin("To improve: drill concurrency");
    const rec = stage({ id: "rec", name: "Recruiter call", status: "done", checkin: note });
    const existing = [applied(), rec];
    const draft = [stage({ id: "n1", name: "Onsite" }), stage({ id: "n2", name: "   " })];

    const out = draftToStages(draft, existing, ids());
    expect(names(out)).toEqual(["Applied", "Recruiter call", "Onsite"]);
    expect(out[1].checkin).toEqual(note);
    expect(statuses(out)).toEqual(["done", "done", "current"]);
  });

  it("keeps a draft row that is also stored exactly once", () => {
    const rec = stage({ id: "rec", name: "Recruiter call", status: "done" });
    const out = draftToStages([rec, stage({ id: "n1", name: "Onsite" })], [applied(), rec], ids());
    expect(names(out)).toEqual(["Applied", "Recruiter call", "Onsite"]);
  });
});

describe("loopUnknown", () => {
  // M6
  it("is true only for a live journey with nothing upcoming", () => {
    expect(loopUnknown(j({ stages: [applied()] }))).toBe(true);
    expect(loopUnknown(j({ stages: [applied(), stage({ id: "b" })] }))).toBe(false);
    expect(loopUnknown(j({ stages: [applied()], outcome: "offer" }))).toBe(false);
  });
});

describe("seedPrep", () => {
  const target = stage({ id: "s1" });
  const past = (over: Partial<Journey>) =>
    j({ id: "j2", company: "Notion", updated_at: "2026-09-10T00:00:00Z", ...over });

  // M7
  it("ignores the journey being viewed", () => {
    const own = j({
      id: "j1",
      stages: [stage({ id: "x", checkin: checkin("To improve: mine") })],
    });
    expect(seedPrep([own], "j1", target)).toEqual([]);
    expect(seedPrep([{ ...own, id: "j2" }], "j1", target).map((p) => p.text)).toEqual(["mine"]);
  });

  // M8
  it("reads retro.change and the 'To improve:' half of a check-in sentence", () => {
    const withRetro = past({ retro: { keep: "kept", change: "drill concurrency", next: null } });
    expect(seedPrep([withRetro], "j1", target).map((p) => p.text)).toEqual(["drill concurrency"]);

    const withNote = past({
      stages: [stage({ id: "x", checkin: checkin("Went well: A · To improve: B") })],
    });
    expect(seedPrep([withNote], "j1", target).map((p) => p.text)).toEqual(["B"]);
  });

  // M9
  it("de-dupes, caps at the limit, and skips what is already prepped", () => {
    const a = past({ id: "j2", retro: { keep: null, change: "same thing", next: null } });
    const b = past({ id: "j3", retro: { keep: null, change: "same thing", next: null } });
    expect(seedPrep([a, b], "j1", target)).toHaveLength(1);

    const many = past({
      id: "j4",
      stages: [
        stage({
          id: "x",
          checkin: checkin(
            ["To improve: one", "To improve: two", "To improve: three", "To improve: four"].join(
              " · ",
            ),
          ),
        }),
        stage({ id: "y", checkin: checkin("To improve: five") }),
      ],
    });
    expect(seedPrep([many], "j1", target).map((p) => p.text)).toEqual(["one", "two", "three"]);

    const prepped = stage({ id: "s1", prep: [{ text: "One", done: false }] });
    expect(seedPrep([many], "j1", prepped).map((p) => p.text)).toEqual(["two", "three", "four"]);
  });

  // M10
  it("tags every item with the journey it came from, not done", () => {
    const a = past({ id: "j2", retro: { keep: null, change: "alpha", next: null } });
    const b = past({ id: "j3", updated_at: "2026-09-05T00:00:00Z" , retro: { keep: null, change: "beta", next: null } });
    expect(seedPrep([b, a], "j1", target)).toEqual([
      { text: "alpha", done: false, from_journey_id: "j2" },
      { text: "beta", done: false, from_journey_id: "j3" },
    ]);
  });
});

describe("datetime-local helpers", () => {
  // M11
  it("round-trips a wall-clock value, with zero padding", () => {
    for (const v of ["2026-09-20T14:00", "2026-01-03T09:05", "2026-12-31T00:00"]) {
      expect(toLocalInput(fromLocalInput(v))).toBe(v);
    }
    expect(fromLocalInput("")).toBe(null);
    expect(toLocalInput(null)).toBe("");
    expect(toLocalInput("2026-01-03T09:05:00Z", UTC)).toBe("2026-01-03T09:05");
  });
});

describe("relativeWhen", () => {
  // M12
  it("counts calendar days and gives up past a fortnight", () => {
    expect(relativeWhen("2026-09-21T18:00:00Z", now, UTC)).toBe("today");
    expect(relativeWhen("2026-09-22T09:00:00Z", now, UTC)).toBe("tomorrow");
    expect(relativeWhen("2026-09-23T09:00:00Z", now, UTC)).toBe("in 2 days");
    expect(relativeWhen("2026-10-04T09:00:00Z", now, UTC)).toBe("in 13 days");
    expect(relativeWhen("2026-10-05T09:00:00Z", now, UTC)).toBe(null);
    expect(relativeWhen("2026-09-21T11:00:00Z", now, UTC)).toBe(null);
  });
});
