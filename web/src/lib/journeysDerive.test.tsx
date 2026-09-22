import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import { JourneyTrack } from "@/components/warm/JourneyTrack";
import { stagesToTrack } from "@/lib/journeysDerive";
import type { Journey, JourneyStage } from "@/lib/types";

// The seam: the board feeds the unchanged marketing-hero component. What a
// runtime test can assert is that the derived stages render as that component
// expects; that `stagesToTrack` returns the very `TrackStage[]` type the hero
// declares is enforced by `tsc`, not here.
function stage(id: string, status: JourneyStage["status"]): JourneyStage {
  return { id, name: id, status, scheduled_at: null, format: null, who: null, checkin: null, questions: [], prep: [] };
}

const journey: Journey = {
  id: "j1",
  user_id: "u1",
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
  company: "Shopify",
  role: "Staff Engineer",
  source: "hermes",
  application_id: "app-1",
  job_url: null,
  stages: [stage("applied", "done"), stage("recruiter", "done"), stage("design", "current"), stage("hm", "upcoming")],
  outcome: "in_progress",
  ended_at_stage_id: null,
  retro: null,
};

describe("board output drives the shared track", () => {
  it("renders one current step and a fixed column per stage", () => {
    const now = new Date("2026-09-21T12:00:00Z");
    const html = renderToStaticMarkup(
      <JourneyTrack stages={stagesToTrack(journey, now, { timeZone: "UTC" })} layout="fixed" />,
    );
    expect(html.match(/aria-current="step"/g)).toHaveLength(1);
    expect(html.match(/width:112px/g)).toHaveLength(journey.stages.length);
    expect(html.match(/role="listitem"/g)).toHaveLength(journey.stages.length);
    expect(html).toContain("you&#x27;re here");
    expect(html).toContain("not booked");
  });
});
