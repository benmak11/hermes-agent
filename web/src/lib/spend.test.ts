// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/apiError";
import {
  rateProvenance,
  scoringCap,
  scoringCapMessage,
  spendConfirmation,
  usdRange,
  zeroGrantReason,
} from "@/lib/spend";

const ESTIMATE = {
  action: "score_backlog",
  units: 200,
  unit: "job",
  usd_low: 0.98,
  usd_high: 3.92,
  rate_usd: 0.0098,
  rate_source: "measured_2026_08_23",
  rate_sample: 0,
  caps: {
    per_cycle: 200,
    per_day: 400,
    remaining_cycle: 200,
    remaining_day: 400,
  },
};

function err(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }), "req-1");
}

describe("spendConfirmation", () => {
  it("reads the quote out of a 402", () => {
    const got = spendConfirmation(
      err(402, {
        needs_confirmation: true,
        action: "score_backlog",
        estimate: ESTIMATE,
        confirm_token: "abc123",
      }),
    );
    expect(got?.confirm_token).toBe("abc123");
    expect(got?.estimate.units).toBe(200);
  });

  it("ignores anything that is not a 402", () => {
    // 409 already means "wrong application state" elsewhere in this API, and
    // treating one as a spend prompt would show a confirm sheet for a
    // conflict the user cannot resolve by paying.
    //
    // The body here is a *complete, valid* confirmation on purpose: an
    // earlier version of this test sent a stub, so the malformed-body guard
    // rejected it and the status check itself was never exercised. Only a
    // body that would otherwise parse can prove the status is what decides.
    const wellFormed = {
      needs_confirmation: true,
      action: "score_backlog",
      estimate: ESTIMATE,
      confirm_token: "abc123",
    };
    expect(spendConfirmation(err(409, wellFormed))).toBeNull();
    expect(spendConfirmation(err(200, wellFormed))).toBeNull();
    expect(spendConfirmation(err(403, "refused"))).toBeNull();
    expect(spendConfirmation(new Error("network"))).toBeNull();
  });

  it("refuses a malformed 402 rather than asking the user to approve a blank", () => {
    expect(spendConfirmation(err(402, { needs_confirmation: true }))).toBeNull();
    expect(
      spendConfirmation(
        err(402, { needs_confirmation: true, confirm_token: "x", estimate: {} }),
      ),
    ).toBeNull();
    expect(spendConfirmation(new ApiError(402, "not json", "r"))).toBeNull();
  });
});

describe("usdRange", () => {
  it("is always a range when the ends differ", () => {
    expect(usdRange(0.98, 3.92)).toBe("$0.98 – $3.92");
  });

  it("collapses only when the two ends round to the same cent", () => {
    expect(usdRange(0.004, 0.0041)).toBe("$0.00");
  });
});

describe("rateProvenance", () => {
  it("says so when the rate is the user's own history", () => {
    expect(rateProvenance("your_last_runs", 312, 0.0104)).toContain("your last 312");
  });

  it("names the measurement date when it is the fallback", () => {
    // The date has to survive into the string: a number whose provenance is
    // "trust us" is not one the user can check.
    expect(rateProvenance("measured_2026_08_23", 0, 0.0098)).toContain("2026-08-23");
  });

  it("finds the date in a fallback source that names more than the date", () => {
    // The per-rated-job fallback is "measured_rated_2026_08_23". Stripping a
    // fixed "measured_" prefix rendered "rated-2026-08-23" as the date.
    const text = rateProvenance("measured_rated_2026_08_23", 0, 0.0193);
    expect(text).toContain("our 2026-08-23 measurement");
    expect(text).not.toContain("rated-");
  });

  it("never shows an internal identifier when there is no date", () => {
    expect(rateProvenance("estimated_no_ledger_yet", 0, 0.0016)).not.toContain(
      "estimated_no_ledger_yet",
    );
  });
});

describe("zeroGrantReason", () => {
  it("names the cap that is empty, the day when both are", () => {
    expect(zeroGrantReason({ remaining_cycle: 0, remaining_day: 200 })).toBe("cycle");
    expect(zeroGrantReason({ remaining_cycle: 200, remaining_day: 0 })).toBe("day");
    expect(zeroGrantReason({ remaining_cycle: 0, remaining_day: 0 })).toBe("day");
  });
});

describe("scoringCap", () => {
  const CAP = {
    reason: "scoring_cap",
    cap: "cycle",
    per_cycle: 3,
    per_day: 10,
    remaining_cycle: 0,
    remaining_day: 7,
    resets_at: "2026-10-07T00:00:00+00:00",
  };

  it("reads the cap and reset out of a 429", () => {
    const got = scoringCap(err(429, CAP));
    expect(got?.cap).toBe("cycle");
    expect(got?.resets_at).toBe(CAP.resets_at);
  });

  it("is not a confirmation: a 429 never opens the spend sheet", () => {
    expect(spendConfirmation(err(429, CAP))).toBeNull();
  });

  it("ignores anything that is not a scoring-cap 429", () => {
    expect(scoringCap(err(402, CAP))).toBeNull();
    expect(scoringCap(err(429, { ...CAP, reason: "discovery_cap" }))).toBeNull();
    expect(scoringCap(err(429, { ...CAP, cap: "week" }))).toBeNull();
    expect(scoringCap(new ApiError(429, "not json", "req-1"))).toBeNull();
    expect(scoringCap(new Error("429"))).toBeNull();
  });

  it("renders the reason and the reset in local time", () => {
    const now = Date.parse("2026-10-06T15:00:00Z");
    const local = new Date(CAP.resets_at).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
    const cycle = scoringCapMessage(scoringCap(err(429, CAP))!, now);
    expect(cycle).toContain("This search's scoring budget is used up");
    expect(cycle).toContain(`resets at ${local}`);

    const day = scoringCapMessage(scoringCap(err(429, { ...CAP, cap: "day" }))!, now);
    expect(day).toContain("Today's scoring budget is used up");
    expect(day).toContain(`resets at ${local}`);
  });
});
