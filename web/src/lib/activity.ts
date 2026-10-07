// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/**
 * Turning `GET /activity` into words, a visual and a poll cadence — honestly.
 *
 * The rule this module exists to enforce: **the UI never claims work is
 * happening when it isn't, and never draws progress it cannot measure.** This
 * product has shipped that bug three times (a "Scoring in progress" banner over
 * a queue with no consumer, a timer-driven onboarding bar, a tint asserting a
 * verdict the data couldn't support), so the mapping is pure, exhaustive and
 * unit-tested rather than decided inline in a component — the same shape
 * `app/applications/status.ts` already set for application statuses.
 *
 * Three invariants, each pinned by a test:
 *
 * 1. `visual: "bar"` is reachable **only** when the server sent both `done` and
 *    `total` as numbers. Everything else gets shimmer, a spinner, or nothing.
 * 2. No idle or terminal state gets a spinner or a present-progressive verb.
 *    "Waiting to score 412 jobs" is a lie when nothing will score them.
 * 3. Durations are **elapsed**, never estimated. `elapsed` counts up from
 *    `since`; `nextLine` quotes a real scheduled time from `next_at`. Neither
 *    guesses at how long anything will take — for most of these legs there is
 *    no completed run to guess from.
 */

/** The nine states `GET /activity` can report. Closed union on purpose. */
export type ActivityState =
  | "never_started"
  | "idle_scheduled"
  | "idle_unscheduled"
  | "queued"
  | "running"
  | "waiting_external"
  | "finished"
  | "failed"
  | "stalled";

export type ActivityKind =
  | "discovery"
  | "sweep"
  | "scoring"
  | "batch_scoring"
  | "extract"
  | "tailoring"
  | "submission";

export type ActivityItem = {
  kind: ActivityKind;
  state: ActivityState;
  since: string | null;
  next_at: string | null;
  ref: Record<string, string> | null;
  /** Both or neither, enforced server-side. The only licence to draw a bar. */
  done: number | null;
  total: number | null;
  detail: Record<string, unknown>;
};

/**
 * One cap's state: how much of it is spent, and when it rolls.
 *
 * `limit` and `remaining` are `null` — **not `0`** — when that cap is off.
 * "Unlimited" is not a quantity, and a `0` here renders as "no searches left"
 * when the truth is "no limit". That is the search cap's case:
 * `DISCOVERY_RUNS_PER_WEEK=0` is a kill switch, and
 * `tools/discovery/budget.py`'s `remaining() -> int | None` says so in its
 * own type. The rating cap has **no kill switch today** —
 * `SCORING_BUDGET_PER_DAY=0` means *no ratings*, not *no cap*, and renders
 * honestly as "0 of 0" — so `ratings.limit` is always a number in practice.
 * It is typed nullable anyway so no consumer hard-codes an assumption that a
 * future kill switch would break.
 */
export type AllowanceBlock = {
  used: number;
  limit: number | null;
  remaining: number | null;
  /** ISO instant. The server never formats a local time; `resetsLine` does. */
  resets_at: string;
};

/**
 * The two caps, with **two independent reset instants**.
 *
 * Searches roll next Monday 00:00 UTC, ratings next UTC midnight. They
 * coincide one day in seven and differ the other six, so a surface that shows
 * both has to show both — one shared "resets at" would be wrong most of the
 * week.
 */
/**
 * Ratings carry a second, per-search window.
 *
 * `remaining` is what the next reservation would actually grant, i.e. the
 * smaller of the daily and per-search remainders. `remaining_cycle` is the
 * per-search one on its own. Both roll at `resets_at`.
 */
export type RatingsAllowance = AllowanceBlock & { remaining_cycle: number };

export type Allowance = {
  searches: AllowanceBlock;
  ratings: RatingsAllowance;
};

export type ActivityResponse = {
  polled_at: string;
  next_tick_at: string;
  items: ActivityItem[];
  allowance: Allowance;
  committed: { usd_low: number; usd_high: number; runs: number };
};

/**
 * How the state should read, at a glance.
 *
 * `quiet` is not `none`: `none` means nothing is drawn at all, `quiet` means a
 * deliberately still marker — the honest rendering of "this is over" or "this
 * is stuck" — so a caller cannot accidentally animate a dead state.
 */
export type ActivityVisual = "none" | "shimmer" | "spinner" | "bar" | "quiet";

export type ActivityTone = "active" | "waiting" | "idle" | "done" | "problem";

export type ActivityView = {
  tone: ActivityTone;
  /** What is (or isn't) happening, in the user's words. */
  verb: string;
  /** Time since `since`, counted up. Empty when there is nothing to count. */
  elapsed: string;
  /** "Next check at 13:00" — only ever from a real `next_at`. */
  nextLine: string;
  visual: ActivityVisual;
};

/**
 * The present-progressive label, used **only** by the `running` branch.
 *
 * Exported so the test can assert the negative: no other state may render one
 * of these strings, which is the concrete form of "the UI must not claim work is
 * happening when it isn't".
 */
export const ACTIVITY_LABELS: Record<ActivityKind, string> = {
  discovery: "finding new roles",
  sweep: "re-checking postings",
  scoring: "scoring your matches",
  batch_scoring: "scoring your matches",
  extract: "reading your resume",
  tailoring: "writing your resume",
  submission: "sending your application",
};

/**
 * The noun for a state that is *not* running — deliberately not the `-ing`
 * form.
 *
 * A present participle is itself a claim that something is underway, which is
 * how "Scoring in progress" ended up over an empty queue. Idle and terminal
 * states get a noun phrase instead, and that is a design constraint rather than
 * a wording preference.
 */
export const ACTIVITY_NOUNS: Record<ActivityKind, string> = {
  discovery: "new roles",
  sweep: "posting checks",
  scoring: "match scoring",
  batch_scoring: "match scoring",
  extract: "resume reading",
  tailoring: "resume writing",
  submission: "application sending",
};

function hhmm(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** "4s" / "3m" / "2h 10m" — elapsed since `iso`, never an estimate. */
export function elapsedSince(iso: string | null, now: number): string {
  if (!iso) return "";
  const started = new Date(iso).getTime();
  if (Number.isNaN(started)) return "";
  const secs = Math.floor((now - started) / 1000);
  if (secs < 0) return "";
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

/**
 * The honest render of one activity item.
 *
 * `now` is a parameter, not `Date.now()` inside, so elapsed time is testable
 * and so a component renders from one clock reading rather than several.
 */
export function activityView(item: ActivityItem, now: number): ActivityView {
  const label = ACTIVITY_LABELS[item.kind] ?? item.kind;
  const noun = ACTIVITY_NOUNS[item.kind] ?? item.kind;
  const elapsed = elapsedSince(item.since, now);
  const at = item.next_at ? hhmm(item.next_at) : "";

  switch (item.state) {
    case "running": {
      // The one state that may draw a bar — and only if the server measured
      // both ends of it. A numerator with an unknown denominator is a
      // fabricated percentage, so it falls back to a shimmer.
      const measured =
        typeof item.done === "number" && typeof item.total === "number";
      return {
        tone: "active",
        verb: measured ? `${label} — ${item.done} of ${item.total}` : label,
        elapsed,
        nextLine: "",
        visual: measured ? "bar" : "shimmer",
      };
    }
    case "queued":
      return {
        tone: "active",
        // Not "writing your resume…": nothing has picked it up yet, and saying
        // otherwise is the original bug.
        verb: `${noun} is queued`,
        elapsed,
        nextLine: "",
        visual: "spinner",
      };
    case "waiting_external":
      return {
        tone: "waiting",
        // **We are doing nothing.** Google has the batch, and the next thing
        // that can possibly change is the hourly check — so that, and never a
        // percent, is what gets drawn.
        verb: "waiting on Google's batch service",
        elapsed,
        nextLine: at ? `next check at ${at}` : "",
        visual: "quiet",
      };
    case "idle_scheduled":
      return {
        tone: "idle",
        verb: at ? `${noun} next at ${at}` : `${noun} is scheduled`,
        elapsed: "",
        nextLine: at ? `next at ${at}` : "",
        visual: "none",
      };
    case "idle_unscheduled":
      // **The state the product keeps getting wrong.** Nothing is running and
      // nothing will, so: no spinner, no `-ing` verb, and no next time — there
      // isn't one to quote.
      return {
        tone: "idle",
        verb: `${noun} is off — nothing is scheduled`,
        elapsed: "",
        nextLine: "",
        visual: "none",
      };
    case "never_started":
      return {
        tone: "idle",
        verb: `${noun} hasn't run yet`,
        elapsed: "",
        nextLine: at ? `first run at ${at}` : "",
        visual: "none",
      };
    case "finished":
      return {
        tone: "done",
        verb: `${noun} is up to date`,
        elapsed: "",
        nextLine: at ? `next at ${at}` : "",
        visual: "none",
      };
    case "failed":
      return {
        tone: "problem",
        verb: `${noun} stopped with an error`,
        elapsed: "",
        nextLine: "",
        visual: "quiet",
      };
    case "stalled":
      // Deliberately *not* "running". The claim outlived any process that
      // could still be holding it, so the honest statement is that we do not
      // know — plus how long it has been that way, which is a real measurement.
      return {
        tone: "problem",
        verb: `${noun} stopped responding`,
        elapsed,
        nextLine: at ? `retrying at ${at}` : "",
        visual: "quiet",
      };
  }
}

/**
 * "9 of 14 searches this week", or the honest uncapped form.
 *
 * The uncapped branch says what is *known* (how many ran) and refuses to
 * invent a denominator, which is the same rule `activityView` applies to a
 * progress bar.
 */
export function allowanceLabel(
  block: AllowanceBlock,
  noun: string,
  window: string,
): string {
  return block.limit === null
    ? `${block.used} ${noun} ${window} \u00b7 no limit`
    : `${block.used} of ${block.limit} ${noun} ${window}`;
}

/**
 * "resets at 01:00" / "resets Mon 01:00" — the viewer's own clock.
 *
 * A weekly window is days away, so an hour alone would be ambiguous; a nightly
 * one is not, so a weekday on it would be noise. The instant itself comes from
 * the server and is never re-derived here.
 */
export function resetsLine(iso: string, now: number): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const within24h = at.getTime() - now < 24 * 3600 * 1000;
  const day = within24h
    ? ""
    : `${at.toLocaleDateString([], { weekday: "short" })} `;
  return `resets ${within24h ? "at " : ""}${day}${hhmm(iso)}`;
}

/**
 * What to say under the ratings figure: always the reset instant.
 *
 * When the per-search window is empty while the day still has room, the
 * "N of M today" figure alone would read as ratings left, so the line says
 * none are. `used < limit` is that test, and it is why `used` has to be the
 * real stored counter rather than `limit - remaining`.
 */
export function ratingsResetLine(block: RatingsAllowance, now: number): string {
  const dayHasRoom = block.limit === null || block.used < block.limit;
  const reset = resetsLine(block.resets_at, now);
  if (block.remaining === 0 && block.remaining_cycle === 0 && dayHasRoom) {
    return reset ? `none left for now \u00b7 ${reset}` : "none left for now";
  }
  return reset;
}

/** Poll cadences, by what can actually change. */
export const POLL_ACTIVE_MS = 3000;
export const POLL_EXTERNAL_MS = 60000;
export const POLL_SCHEDULED_MS = 60000;

/**
 * How often to re-ask `/activity`, or `false` for "don't".
 *
 * Replaces four ad-hoc `refetchInterval`s (`app/page.tsx`, `profile/page.tsx`,
 * `tracking/page.tsx`, `review/page.tsx`), three of which polled a steady-state
 * screen forever. The rule is what can change and how fast:
 *
 * - a live claim can change in seconds;
 * - a Vertex batch cannot change faster than the hourly resume tick, so polling
 *   it every three seconds is 1,200 pointless requests an hour;
 * - a leg that is **scheduled** will start on its own, with nobody touching
 *   the page. Its start is a real event on a clock we do not own, so a minute
 *   is the cadence: fast enough that a 13:00 cron shows up as work by 13:01,
 *   slow enough to be 60 requests an hour rather than 1,200. Without this the
 *   screen sits at "next at 13:00" through 13:00, 13:30 and 14:00 — honest,
 *   but staler than the hard-coded 30s interval this function replaced, and
 *   that would be a freshness regression inherited by accident.
 * - nothing else can change without the user doing something, and a user action
 *   invalidates the query anyway. So: no polling at all. `false` rather than a
 *   long interval, because "we are still checking" is itself an implied claim
 *   that something might be happening.
 *
 * `idle_scheduled` **with a real `next_at`** is the only idle state that polls.
 * `idle_unscheduled`, `never_started`, `finished`, `failed` and `stalled` all
 * mean nothing is coming, and polling them would be that implied claim.
 *
 * Deliberately not conditioned on how far away `next_at` is: a distance test
 * cannot re-evaluate itself. Returning `false` stops the timer, so a schedule
 * four hours out would never come back into range on its own, and the screen
 * that most needs the eventual refetch is the one that would never get it.
 */
export function pollMs(items: ActivityItem[]): number | false {
  let interval: number | false = false;
  for (const item of items) {
    if (item.state === "running" || item.state === "queued") {
      return POLL_ACTIVE_MS; // nothing polls faster; no need to look further
    }
    if (item.state === "waiting_external") interval = POLL_EXTERNAL_MS;
    else if (item.state === "idle_scheduled" && item.next_at && interval === false) {
      interval = POLL_SCHEDULED_MS;
    }
  }
  return interval;
}
