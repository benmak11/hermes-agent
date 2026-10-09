import { describe, expect, it } from "vitest";

import {
  type BoardHealth,
  type BoardRow,
  boardsSummary,
  boardsView,
  isFailing,
  listLabel,
  notFoundDays,
  outcomeLabel,
  sortBoards,
  stateLabel,
} from "@/lib/adminBoards";
import { ApiError } from "@/lib/apiError";

function board(over: Partial<BoardRow>): BoardRow {
  return {
    platform: "greenhouse",
    slug: "acme",
    name: null,
    list: "known",
    paused: false,
    blocklisted: false,
    state: "ok",
    last_outcome: "ok",
    last_status: 200,
    failing_since: null,
    not_found_days: 0,
    last_ok_at: null,
    updated_at: null,
    ...over,
  };
}

const TOTALS = { total: 0, ok: 0, failing: 0, not_found: 0, never_checked: 3 };

describe("sortBoards", () => {
  it("puts failing first, then most 404 days, then platform and slug", () => {
    const rows = [
      board({ platform: "ashby", slug: "gamma" }),
      board({ slug: "acme", state: "failing", not_found_days: 1 }),
      board({ platform: "lever", slug: "beta", state: "failing", not_found_days: 4 }),
      board({ slug: "pausedco", state: "failing", not_found_days: 1 }),
      board({ platform: "ashby", slug: "zeta", state: "failing", not_found_days: 0 }),
      board({ slug: "aaa" }),
    ];
    expect(sortBoards(rows).map((r) => `${r.platform}:${r.slug}`)).toEqual([
      "lever:beta",
      "greenhouse:acme",
      "greenhouse:pausedco",
      "ashby:zeta",
      "ashby:gamma",
      "greenhouse:aaa",
    ]);
  });

  it("does not reorder its input", () => {
    const rows = [board({ slug: "b" }), board({ slug: "a", state: "failing" })];
    sortBoards(rows);
    expect(rows.map((r) => r.slug)).toEqual(["b", "a"]);
  });
});

describe("formatting", () => {
  it("summarises the totals in one line", () => {
    expect(
      boardsSummary({ total: 12, ok: 9, failing: 3, not_found: 2, never_checked: 4 }),
    ).toBe("12 boards · 9 ok · 3 failing · 2 not found · 4 never checked");
    expect(boardsSummary({ ...TOTALS, total: 1, ok: 1, never_checked: 0 })).toBe(
      "1 board · 1 ok · 0 failing · 0 not found · 0 never checked",
    );
  });

  it("labels the outcome with its status when there is one", () => {
    expect(outcomeLabel(board({ last_outcome: "not_found", last_status: 404 }))).toBe(
      "Not found · 404",
    );
    expect(outcomeLabel(board({ last_outcome: "timeout", last_status: null }))).toBe(
      "Timeout",
    );
    expect(outcomeLabel(board({ last_outcome: "brand_new", last_status: 418 }))).toBe(
      "brand_new · 418",
    );
    expect(outcomeLabel(board({ last_outcome: null, last_status: null }))).toBe("—");
  });

  it("names the list and flags paused and blocklisted boards", () => {
    expect(listLabel(board({}))).toBe("Known");
    expect(listLabel(board({ list: "unvetted" }))).toBe("Unvetted");
    expect(listLabel(board({ paused: true }))).toBe("Known · paused");
    expect(listLabel(board({ list: "none", blocklisted: true }))).toBe(
      "Not listed · blocklisted",
    );
  });

  it("labels the state and 404 days", () => {
    expect(stateLabel(board({ state: "failing" }))).toBe("Failing");
    expect(stateLabel(board({ state: "ok" }))).toBe("OK");
    expect(stateLabel(board({ state: null }))).toBe("—");
    expect(isFailing(board({ state: "failing" }))).toBe(true);
    expect(isFailing(board({ state: "ok" }))).toBe(false);
    expect(notFoundDays(board({ not_found_days: 3 }))).toBe("3");
    expect(notFoundDays(board({ not_found_days: 0 }))).toBe("—");
  });
});

describe("boardsView", () => {
  const data = (boards: BoardRow[]): BoardHealth => ({ totals: TOTALS, boards });

  it("is loading until data arrives", () => {
    expect(boardsView({ loading: true, error: null, data: undefined })).toEqual({
      kind: "loading",
    });
    expect(boardsView({ loading: false, error: null, data: undefined }).kind).toBe(
      "loading",
    );
  });

  it("is empty with no records, keeping the totals", () => {
    expect(boardsView({ loading: false, error: null, data: data([]) })).toEqual({
      kind: "empty",
      totals: TOTALS,
    });
  });

  it("lists the rows failing first", () => {
    const view = boardsView({
      loading: false,
      error: null,
      data: data([board({ slug: "a" }), board({ slug: "b", state: "failing" })]),
    });
    expect(view.kind === "list" && view.rows.map((r) => r.slug)).toEqual(["b", "a"]);
  });

  it("hides on a 404, names the source on a 503, and shows other errors", () => {
    const notFound = new ApiError(404, '{"detail":"Not Found"}', "req-1");
    expect(boardsView({ loading: false, error: notFound, data: undefined })).toEqual({
      kind: "hidden",
    });
    const down = new ApiError(503, '{"detail":"could not read board_health"}', "req-2");
    expect(boardsView({ loading: false, error: down, data: undefined })).toEqual({
      kind: "unavailable",
      source: "board_health",
    });
    const opaque = new ApiError(503, "Service Unavailable", "req-3");
    expect(boardsView({ loading: false, error: opaque, data: undefined })).toEqual({
      kind: "unavailable",
      source: null,
    });
    const boom = new ApiError(500, "boom", "req-4");
    expect(boardsView({ loading: false, error: boom, data: undefined })).toEqual({
      kind: "error",
      message: boom.message,
      requestId: "req-4",
    });
  });
});
