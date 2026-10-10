import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/apiError";
import { extractCap, extractErrorMessage } from "@/lib/extractError";

const RESETS = "2026-10-10T00:00:00+00:00";

function capError(used = 5, resets_at: string = RESETS): ApiError {
  return new ApiError(
    429,
    JSON.stringify({ detail: { reason: "extract_cap", used, per_day: 5, resets_at } }),
    "req-1",
  );
}

describe("extractCap", () => {
  it("reads the daily-extraction 429", () => {
    expect(extractCap(capError())).toEqual({
      reason: "extract_cap",
      used: 5,
      per_day: 5,
      resets_at: RESETS,
    });
  });

  it("ignores other 429s and other statuses", () => {
    const scoring = new ApiError(
      429,
      JSON.stringify({ detail: { reason: "scoring_cap", cap: "day", resets_at: RESETS } }),
      "r",
    );
    expect(extractCap(scoring)).toBeNull();
    expect(extractCap(new ApiError(422, "{}", "r"))).toBeNull();
    expect(extractCap(new Error("429: boom"))).toBeNull();
    expect(extractCap(new ApiError(429, "not json", "r"))).toBeNull();
  });
});

describe("extractErrorMessage", () => {
  it("tells the user how many uploads and when to come back", () => {
    const at = new Date(RESETS).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    expect(extractErrorMessage(capError())).toBe(
      `You've uploaded your résumé 5 times today; try again after ${at}.`,
    );
  });

  it("says once, not 1 times", () => {
    expect(extractErrorMessage(capError(1))).toMatch(/uploaded your résumé once today/);
  });

  it("falls back to tomorrow on an unreadable reset instant", () => {
    expect(extractErrorMessage(capError(5, "soon"))).toMatch(/try again after tomorrow\.$/);
  });

  it("keeps the old message for every other failure", () => {
    expect(extractErrorMessage(new ApiError(422, "Could not parse", "r"))).toBe(
      "Could not parse (request r)",
    );
    expect(extractErrorMessage("weird")).toBe("Something went wrong reading your resume.");
  });
});
