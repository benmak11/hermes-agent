import { describe, expect, it } from "vitest";

import {
  APPLIED_STAGE_ID,
  WEEK_MS,
  applicationToJourneyInput,
  boardSummary,
  legacyToJourney,
  parseLegacy,
  sortForBoard,
  stagesToTrack,
  thisWeek,
  untrackedApplications,
  withinWeek,
  type LegacyJournalEntry,
} from "@/lib/journeysDerive";
import type { Application, Journey, JourneyStage } from "@/lib/types";

const UTC = { timeZone: "UTC" };
// A Monday.
const now = new Date("2026-09-21T12:00:00Z");

function stage(over: Partial<JourneyStage> & Pick<JourneyStage, "id" | "status">): JourneyStage {
  return {
    name: over.id,
    scheduled_at: null,
    format: null,
    who: null,
    checkin: null,
    questions: [],
    prep: [],
    ...over,
  };
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
    stages: [stage({ id: APPLIED_STAGE_ID, name: "Applied", status: "done" })],
    outcome: "in_progress",
    ended_at_stage_id: null,
    retro: null,
    ...over,
  };
}

function app(over: Partial<Application> & Pick<Application, "id" | "status">): Application {
  return {
    user_id: "u1",
    job_id: `job-${over.id}`,
    job_company: "Figma",
    job_title: "Infrastructure Engineer",
    master_bullets: [],
    tailored_bullets: [],
    timeline: [],
    ...over,
  };
}

const iso = (ms: number) => new Date(ms).toISOString();

describe("withinWeek / thisWeek", () => {
  it("thisWeek boundary at exactly 7 days", () => {
    expect(withinWeek(iso(now.getTime() + WEEK_MS - 1), now)).toBe(true);
    expect(withinWeek(iso(now.getTime() + WEEK_MS), now)).toBe(false);
    expect(withinWeek(iso(now.getTime() - 1), now)).toBe(false);
    expect(withinWeek(iso(now.getTime()), now)).toBe(true);

    const at = (ms: number) =>
      thisWeek(
        [j({ stages: [stage({ id: "s", name: "Recruiter", status: "upcoming", scheduled_at: iso(ms) })] })],
        now,
      );
    expect(at(now.getTime() + WEEK_MS - 1)).toEqual([
      { kind: "scheduled", journeyId: "j1", stageId: "s", at: iso(now.getTime() + WEEK_MS - 1), label: "Recruiter · Shopify" },
    ]);
    expect(at(now.getTime() + WEEK_MS)).toEqual([]);
    expect(at(now.getTime() - 1)).toEqual([]);
  });

  it("sorts scheduled items soonest first across journeys", () => {
    const later = iso(now.getTime() + 3 * 24 * 3600 * 1000);
    const sooner = iso(now.getTime() + 1 * 24 * 3600 * 1000);
    const items = thisWeek(
      [
        j({ id: "a", stages: [stage({ id: "x", status: "upcoming", scheduled_at: later })] }),
        j({ id: "b", company: "Anthropic", stages: [stage({ id: "y", status: "current", scheduled_at: sooner })] }),
      ],
      now,
    );
    expect(items.map((i) => (i.kind === "scheduled" ? i.journeyId : i.kind))).toEqual(["b", "a"]);
  });

  it("a done stage without check-in counts once, applied never does", () => {
    const stages = [
      stage({ id: APPLIED_STAGE_ID, name: "Applied", status: "done" }),
      stage({ id: "recruiter", status: "done" }),
      stage({ id: "design", status: "current" }),
    ];
    expect(thisWeek([j({ stages })], now)).toEqual([{ kind: "checkins", count: 1 }]);

    const more = [...stages.slice(0, 2), stage({ id: "tech", status: "done" }), stages[2]];
    expect(thisWeek([j({ stages: more })], now)).toEqual([{ kind: "checkins", count: 2 }]);

    // A check-in already logged does not wait.
    const done = [
      stages[0],
      stage({ id: "recruiter", status: "done", checkin: { rating: 4, tags: [], sentence: null, at: iso(now.getTime()) } }),
      stages[2],
    ];
    expect(thisWeek([j({ stages: done })], now)).toEqual([]);

    // Closed journeys never ask for anything.
    expect(thisWeek([j({ stages, outcome: "rejected" })], now)).toEqual([]);
    expect(thisWeek([j({ stages, outcome: "offer" })], now)).toEqual([]);
  });
});

describe("boardSummary", () => {
  it("buckets talking / waiting / offers / closed", () => {
    const talking = [
      stage({ id: APPLIED_STAGE_ID, status: "done" }),
      stage({ id: "r", status: "current" }),
    ];
    const summary = boardSummary([
      j({ id: "1", stages: talking }),
      j({ id: "2", stages: talking }),
      j({ id: "3" }), // Applied only — all done, nothing current
      j({ id: "4", outcome: "offer" }),
      j({ id: "5", outcome: "rejected" }),
      j({ id: "6", outcome: "withdrawn" }),
    ]);
    expect(summary).toEqual({ talking: 2, waiting: 1, offers: 1, closed: 2 });
  });
});

describe("stagesToTrack", () => {
  it("maps statuses and notes", () => {
    const track = stagesToTrack(
      j({
        stages: [
          stage({ id: "a", name: "Applied", status: "done", scheduled_at: "2026-09-02T09:00:00Z" }),
          stage({ id: "b", name: "Recruiter call", status: "done", checkin: { rating: 4, tags: [], sentence: null, at: "2026-09-10T00:00:00Z" } }),
          stage({ id: "c", name: "System design", status: "current", scheduled_at: "2026-09-25T14:00:00Z" }),
          stage({ id: "d", name: "Hiring manager", status: "upcoming" }),
          stage({ id: "e", name: "Values", status: "upcoming", scheduled_at: "2026-09-24T10:00:00Z" }),
          stage({ id: "f", name: "Decision", status: "upcoming", scheduled_at: "2026-10-30T10:00:00Z" }),
        ],
      }),
      now,
      UTC,
    );
    expect(track.map((t) => t.status)).toEqual(["done", "done", "current", "upcoming", "upcoming", "upcoming"]);
    expect(track.filter((t) => t.status === "current")).toHaveLength(1);
    expect(track.map((t) => [t.note, t.noteTone])).toEqual([
      ["2 Sep", "muted"],
      ["went well", "good"],
      ["Fri · you're here", "accent"],
      ["not booked", "muted"],
      ["Thu 10:00", "muted"],
      ["30 Oct", "muted"],
    ]);
  });

  it("grades check-ins and handles the unrated legacy note", () => {
    const at = "2026-09-10T00:00:00Z";
    const notes = (rating: number | null) =>
      stagesToTrack(
        j({ stages: [stage({ id: "s", status: "done", checkin: { rating, tags: [], sentence: null, at } })] }),
        now,
        UTC,
      )[0];
    expect(notes(5)).toMatchObject({ note: "went great", noteTone: "good" });
    expect(notes(3)).toMatchObject({ note: "felt okay", noteTone: "muted" });
    expect(notes(1)).toMatchObject({ note: "felt rough", noteTone: "muted" });
    expect(notes(null)).toMatchObject({ note: "checked in", noteTone: "muted" });
  });

  it("dates the current node when it is booked beyond this week, or not at all", () => {
    const one = (s: JourneyStage) => stagesToTrack(j({ stages: [s] }), now, UTC)[0];
    expect(one(stage({ id: "s", status: "current" })).note).toBe("you're here");
    expect(one(stage({ id: "s", status: "current", scheduled_at: "2026-10-30T10:00:00Z" })).note).toBe(
      "30 Oct · you're here",
    );
    expect(one(stage({ id: "s", status: "done" })).note).toBeUndefined();
  });
});

describe("legacy import", () => {
  const rejected: LegacyJournalEntry = {
    id: "e1",
    company: "Notion",
    role: "Backend Engineer",
    stages: [
      { id: "s1", name: "Recruiter", status: "done", wentWell: "told my story well" },
      { id: "s2", name: "Technical", status: "done", toImprove: "slower on the whiteboard" },
      { id: "s3", name: "Onsite", status: "upcoming" },
    ],
    sessions: ["system design"],
    outcome: "rejected",
    endedAtStage: "Technical",
    createdAt: 1_700_000_000_000,
  };
  const offer: LegacyJournalEntry = {
    id: "e2",
    company: "Linear",
    role: "Staff Engineer",
    stages: [{ id: "t1", name: "Recruiter", status: "done" }],
    sessions: [],
    outcome: "offer",
    reflection: "kept the stories short",
    createdAt: 1_700_000_000_000,
  };
  const nowIso = now.toISOString();

  it("legacyToJourney round-trips the real shape", () => {
    const out = legacyToJourney(rejected, nowIso);
    expect(out.source).toBe("manual");
    expect(out.application_id).toBeNull();
    expect(out.outcome).toBe("rejected");
    expect(out.ended_at_stage_id).toBe("s2");
    expect(out.stages.map((s) => s.id)).toEqual(["s1", "s2", "s3", "e1:session:0"]);
    expect(out.stages[0].checkin).toEqual({
      rating: null,
      tags: [],
      sentence: "Went well: told my story well",
      at: nowIso,
    });
    expect(out.stages[1].checkin?.sentence).toBe("To improve: slower on the whiteboard");
    expect(out.stages[2].checkin).toBeNull();
    expect(out.stages[3]).toMatchObject({ name: "system design", status: "done", checkin: null });
    expect(out.retro).toBeNull();

    const won = legacyToJourney(offer, nowIso);
    expect(won.retro).toEqual({ keep: "kept the stories short", change: null, next: null });
    expect(won.ended_at_stage_id).toBeNull();
  });

  it("joins both notes into one sentence", () => {
    const out = legacyToJourney(
      { ...offer, stages: [{ id: "x", name: "A", status: "done", wentWell: "w", toImprove: "i" }] },
      nowIso,
    );
    expect(out.stages[0].checkin?.sentence).toBe("Went well: w · To improve: i");
  });

  it("parseLegacy tolerates garbage", () => {
    expect(parseLegacy(null)).toEqual([]);
    expect(parseLegacy("{")).toEqual([]);
    expect(parseLegacy("{}")).toEqual([]);
    expect(parseLegacy(JSON.stringify([offer]))).toEqual([offer]);
  });
});

describe("applicationToJourneyInput", () => {
  it("dates the Applied stage from confirmation, then last_submitted_at, then the timeline", () => {
    const base = app({
      id: "app-1",
      status: "submitted",
      confirmation: { submitted_at: "2026-09-02T09:00:00Z" },
      last_submitted_at: "2026-09-03T09:00:00Z",
      timeline: [{ at: "2026-09-04T09:00:00Z", status: "submitted" }],
    });
    const nowIso = "2026-09-05T09:00:00Z";
    expect(applicationToJourneyInput(base, nowIso).stages[0].scheduled_at).toBe("2026-09-02T09:00:00Z");
    expect(applicationToJourneyInput({ ...base, confirmation: null }, nowIso).stages[0].scheduled_at).toBe(
      "2026-09-03T09:00:00Z",
    );
    expect(
      applicationToJourneyInput({ ...base, confirmation: null, last_submitted_at: null }, nowIso).stages[0]
        .scheduled_at,
    ).toBe("2026-09-04T09:00:00Z");
    expect(
      applicationToJourneyInput({ ...base, confirmation: null, last_submitted_at: null, timeline: [] }, nowIso)
        .stages[0].scheduled_at,
    ).toBe(nowIso);

    const out = applicationToJourneyInput(base, nowIso);
    expect(out.stages[0]).toMatchObject({ id: APPLIED_STAGE_ID, name: "Applied", status: "done" });
    expect(out).toMatchObject({
      source: "hermes",
      application_id: "app-1",
      company: "Figma",
      role: "Infrastructure Engineer",
      outcome: "in_progress",
    });
  });
});

describe("untrackedApplications", () => {
  it("keeps sent applications no journey points at", () => {
    const apps = [
      app({ id: "sent", status: "submitted" }),
      app({ id: "replied", status: "responded" }),
      app({ id: "tracked", status: "submitted" }),
      app({ id: "failed", status: "failed" }),
      app({ id: "drafting", status: "ready_for_review" }),
    ];
    const journeys = [j({ source: "hermes", application_id: "tracked" })];
    expect(untrackedApplications(apps, journeys).map((a) => a.id)).toEqual(["sent", "replied"]);
  });
});

describe("sortForBoard", () => {
  it("ranks live, offer, closed and newest first within a rank", () => {
    const input = [
      j({ id: "closed-old", outcome: "rejected", updated_at: "2026-09-01T00:00:00Z" }),
      j({ id: "live-old", updated_at: "2026-09-01T00:00:00Z" }),
      j({ id: "offer", outcome: "offer", updated_at: "2026-09-10T00:00:00Z" }),
      j({ id: "closed-new", outcome: "withdrawn", updated_at: "2026-09-09T00:00:00Z" }),
      j({ id: "live-new", updated_at: "2026-09-08T00:00:00Z" }),
    ];
    const snapshot = input.map((x) => x.id);
    expect(sortForBoard(input).map((x) => x.id)).toEqual([
      "live-new",
      "live-old",
      "offer",
      "closed-new",
      "closed-old",
    ]);
    expect(input.map((x) => x.id)).toEqual(snapshot);
  });
});
