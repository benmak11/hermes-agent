import { describe, expect, it } from "vitest";
import { deriveTrack } from "@/components/warm/journeyStages";

describe("deriveTrack", () => {
  it("marks exactly one stage current, with done before and upcoming after", () => {
    const track = deriveTrack(["a", "b", "c"], 1);
    expect(track.map((s) => s.status)).toEqual(["done", "current", "upcoming"]);
    expect(track.filter((s) => s.status === "current")).toHaveLength(1);
    expect(track.findIndex((s) => s.status === "current")).toBe(1);
    expect(track.map((s) => s.id)).toEqual(["0", "1", "2"]);
    expect(track.map((s) => s.name)).toEqual(["a", "b", "c"]);
  });

  it("handles the first stage being current", () => {
    const track = deriveTrack(["a", "b", "c"], 0);
    expect(track.map((s) => s.status)).toEqual([
      "current",
      "upcoming",
      "upcoming",
    ]);
  });

  it("handles the last stage being current", () => {
    const track = deriveTrack(["a", "b", "c"], 2);
    expect(track.map((s) => s.status)).toEqual(["done", "done", "current"]);
  });

  it("returns an empty track for no names", () => {
    expect(deriveTrack([], 0)).toEqual([]);
  });
});
