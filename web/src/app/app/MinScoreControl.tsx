// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

// The review queue's "Min score" slider, colocated with `/app` so it can be
// rendered in a unit test. While the exploration sample is on the slider is
// not a strict floor; the server's `sampled` count makes the control say so.
// It never says which jobs: no card is marked.

/**
 * The server's `sampled` count, capped by how many cards on screen still
 * score under `minScore`. Decisions drop cards from the cached response
 * without refetching it, so the raw count would go on claiming cards the
 * user has already cleared. Scores are on the cards, so this reveals nothing.
 */
export function liveSampled(
  sampled: number | undefined,
  scores: number[],
  minScore: number,
): number | undefined {
  if (sampled === undefined) return undefined;
  return Math.min(sampled, scores.filter((s) => s < minScore).length);
}

/** "plus 2 lower matches, sampled", or null when nothing was sampled. */
export function sampledNote(sampled: number | undefined): string | null {
  if (!sampled || sampled <= 0) return null;
  return `plus ${sampled} lower ${sampled === 1 ? "match" : "matches"}, sampled`;
}

export function MinScoreControl({
  minScore,
  sampled,
  onChange,
}: {
  minScore: number;
  /** `sampled` from `GET /jobs/pending`; absent when nothing was sampled. */
  sampled: number | undefined;
  onChange: (value: number) => void;
}) {
  const note = sampledNote(sampled);
  return (
    <label
      className="flex h-[38px] items-center gap-2.5 rounded-[11px] border px-3.5"
      style={{ background: "var(--surface-warm)", borderColor: "#e8dacb" }}
    >
      <span
        className="whitespace-nowrap text-[12px] font-semibold"
        style={{ color: "var(--ink-4)" }}
      >
        Min score
      </span>
      <input
        type="range"
        min={0}
        max={100}
        value={minScore}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-[130px] accent-[var(--terracotta)]"
      />
      <span
        className="w-5 text-right text-[13px] font-bold tabular-nums"
        style={{ color: "var(--ink)" }}
      >
        {minScore}
      </span>
      {note && (
        <span
          className="whitespace-nowrap text-[12px]"
          style={{ color: "var(--ink-4)" }}
        >
          · {note}
        </span>
      )}
    </label>
  );
}
