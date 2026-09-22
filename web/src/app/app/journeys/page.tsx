// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { TopNav } from "@/components/TopNav";
import { CompanyTile, tileHue } from "@/components/warm/CompanyTile";
import { JourneyTrack } from "@/components/warm/JourneyTrack";
import { Pill } from "@/components/warm/Pill";
import { SERIF } from "@/components/warm/styles";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { JOURNEYS_KEY, importLegacyJournal, useJourneyMutations, useJourneys } from "@/lib/journeys";
import {
  advanceStages,
  applicationToJourneyInput,
  boardSummary,
  currentStage,
  manualJourneyInput,
  nextUp,
  shortDate,
  sortForBoard,
  stagesToTrack,
  thisWeek,
  untrackedApplications,
  weekdayTime,
  withinWeek,
} from "@/lib/journeysDerive";
import type { Application, Journey, JourneyStage } from "@/lib/types";
import { initial } from "@/lib/ui";

/**
 * Warm Flow screen 16 — the journey board. One row per hiring process past
 * "applied", each a node track rendered by the same `<JourneyTrack>` the
 * marketing hero uses (fixed 112px columns here, fluid there). The "This
 * week" strip answers "what do I have to do" before the board answers "where
 * am I everywhere". Sent applications no journey tracks yet appear as
 * single-node Applied rows with a "Track this" action.
 */

type AppsResponse = { applications: Application[] };

const CARD = {
  background: "var(--surface-warm)",
  borderColor: "var(--border-warm-hair)",
} as const;

function newStage(name: string, status: JourneyStage["status"]): JourneyStage {
  return {
    id: crypto.randomUUID(),
    name,
    status,
    scheduled_at: null,
    format: null,
    who: null,
    checkin: null,
    questions: [],
    prep: [],
  };
}

export default function JourneysPage() {
  const { user, loading } = useAuth();
  const queryClient = useQueryClient();
  const journeysQuery = useJourneys(!!user);
  const { data: appsData } = useQuery({
    queryKey: ["applications"],
    queryFn: () => apiFetch<AppsResponse>("/applications"),
    enabled: !!user,
  });
  const { create, save, remove } = useJourneyMutations();
  const [showClosed, setShowClosed] = useState(false);
  const [adding, setAdding] = useState(false);

  // The legacy localStorage journal moves to the server once, and only after
  // a successful empty list (never on error — see importLegacyJournal).
  const imported = useRef(false);
  const serverEmpty = journeysQuery.isSuccess && journeysQuery.data.journeys.length === 0;
  useEffect(() => {
    if (!user || !serverEmpty || imported.current) return;
    imported.current = true;
    importLegacyJournal(user.uid, 0)
      .then((n) => {
        if (n > 0) queryClient.invalidateQueries({ queryKey: JOURNEYS_KEY });
      })
      .catch(() => {
        // Already logged; the un-posted remainder is back in localStorage.
      });
  }, [user, serverEmpty, queryClient]);

  if (loading || !user) {
    return (
      <>
        <TopNav section="journeys" />
        <main className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
          Loading…
        </main>
      </>
    );
  }

  const now = new Date();
  const journeys = journeysQuery.data?.journeys ?? [];
  const apps = appsData?.applications ?? [];
  const summary = boardSummary(journeys);
  const sorted = sortForBoard(journeys);
  const live = sorted.filter((j) => j.outcome === "in_progress");
  const offers = sorted.filter((j) => j.outcome === "offer");
  const closed = sorted.filter((j) => j.outcome === "rejected" || j.outcome === "withdrawn");
  const untracked = untrackedApplications(apps, journeys);
  const week = thisWeek(journeys, now);

  return (
    <>
      <TopNav section="journeys" />
      <main className="mx-auto w-full max-w-[1180px] flex-1 px-[30px] pt-[26px] pb-7">
        <div className="flex flex-wrap items-end justify-between gap-5">
          <div>
            <h1
              className="text-[30px] font-normal leading-[1.15]"
              style={{ fontFamily: SERIF, color: "var(--ink)" }}
            >
              Your journeys
            </h1>
            <div className="mt-2 text-[13px]" style={{ color: "var(--ink-4)" }}>
              <b style={{ color: "var(--terracotta)" }}>{summary.talking}</b> talking to someone ·{" "}
              <b style={{ color: "var(--ink)" }}>{summary.waiting}</b> waiting to hear back ·{" "}
              <b style={{ color: "var(--sage)" }}>{summary.offers}</b>{" "}
              {summary.offers === 1 ? "offer" : "offers"} ·{" "}
              <b style={{ color: "#a3927f" }}>{summary.closed}</b> closed
            </div>
          </div>
          <div className="flex gap-2.5">
            {summary.closed > 0 && (
              <button
                className="wm-ghost h-[38px] rounded-[12px] border px-[15px] text-[13px] font-semibold"
                style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
                onClick={() => setShowClosed((v) => !v)}
              >
                Closed journeys · {summary.closed}
              </button>
            )}
            <button
              className="wm-cta h-[38px] rounded-[12px] px-4 text-[13px] font-semibold"
              onClick={() => setAdding(true)}
            >
              + Add an opportunity
            </button>
          </div>
        </div>

        {adding && (
          <AddOpportunity
            busy={create.isPending}
            onClose={() => setAdding(false)}
            onAdd={(company, role) => {
              create.mutate(manualJourneyInput(company, role, new Date().toISOString()));
              setAdding(false);
            }}
          />
        )}

        <section
          className="mt-[22px] flex items-center gap-4 rounded-[16px] border px-5 py-[15px]"
          style={{ background: "var(--terracotta-tint)", borderColor: "var(--border-warm)" }}
        >
          <span
            className="flex-none text-[10.5px] font-bold uppercase tracking-[0.12em]"
            style={{ color: "var(--terracotta)" }}
          >
            This week
          </span>
          <div className="flex flex-1 flex-wrap gap-2.5">
            {week.length === 0 && (
              <WeekChip>
                <span style={{ color: "#a3927f" }}>Nothing booked this week</span>
              </WeekChip>
            )}
            {week.map((item) =>
              item.kind === "scheduled" ? (
                <WeekChip key={`${item.journeyId}:${item.stageId}`}>
                  <b style={{ color: "var(--ink)" }}>{weekdayTime(item.at)}</b> {item.label}
                </WeekChip>
              ) : (
                <span
                  key="checkins"
                  className="flex items-center gap-[9px] rounded-[11px] px-[13px] py-[7px] text-[12.5px] font-semibold"
                  style={{
                    background: "var(--surface-warm)",
                    border: "1px dashed #d9c4a8",
                    color: "var(--terracotta-d)",
                  }}
                >
                  ✎ {item.count} check-in{item.count === 1 ? "" : "s"} waiting for you
                </span>
              ),
            )}
          </div>
        </section>

        {journeysQuery.isError && (
          <p className="mt-4 text-[13px]" style={{ color: "var(--brick)" }}>
            {"Couldn't load your journeys."}
          </p>
        )}

        {journeysQuery.isSuccess && journeys.length === 0 && untracked.length === 0 && (
          <div className="mt-4 rounded-[18px] border px-6 py-8 text-center" style={CARD}>
            <p className="text-[13.5px] leading-relaxed" style={{ color: "var(--ink-4)" }}>
              Nothing past applied yet. When an application turns into a call, add it here — or
              track one of your applications below.
            </p>
          </div>
        )}

        {live.map((j, i) => (
          <LiveRow
            key={j.id}
            journey={j}
            now={now}
            first={i === 0}
            onSave={(next) => save.mutate(next)}
            error={save.isError && save.variables?.id === j.id ? String(save.error) : null}
          />
        ))}

        {untracked.map((a, i) => (
          <AppliedRow
            key={a.id}
            app={a}
            first={live.length === 0 && i === 0}
            onTrack={() => create.mutate(applicationToJourneyInput(a, new Date().toISOString()))}
          />
        ))}

        {offers.map((j) => (
          <OfferRow key={j.id} journey={j} />
        ))}

        {showClosed &&
          closed.map((j) => (
            <ClosedRow
              key={j.id}
              journey={j}
              onRemove={() => {
                if (window.confirm("Remove this journey? This can't be undone.")) remove.mutate(j.id);
              }}
            />
          ))}

        <p className="mt-5 text-center">
          <Link href="/app/tracking" className="text-[11px] font-medium" style={{ color: "#a3927f" }}>
            your applications ↗
          </Link>
        </p>
      </main>
    </>
  );
}

function WeekChip({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="flex items-center gap-[9px] rounded-[11px] border px-[13px] py-[7px] text-[12.5px]"
      style={{
        background: "var(--surface-warm)",
        borderColor: "var(--border-warm)",
        color: "var(--ink-2)",
      }}
    >
      {children}
    </span>
  );
}

function RowTitle({ role, company, size = 15.5 }: { role: string; company: string; size?: number }) {
  return (
    <span className="min-w-0 flex-1 font-bold" style={{ color: "var(--ink)", fontSize: size }}>
      {role}{" "}
      <span className="font-normal" style={{ color: "var(--ink-4)" }}>
        · {company}
      </span>
    </span>
  );
}

function SourcePill({ source }: { source: Journey["source"] }) {
  return source === "hermes" ? (
    <Pill tone="accent">Hermes applied for you</Pill>
  ) : (
    <Pill tone="muted">You added this one</Pill>
  );
}

function LiveRow({
  journey: j,
  now,
  first,
  onSave,
  error,
}: {
  journey: Journey;
  now: Date;
  first: boolean;
  onSave: (next: Journey) => void;
  error: string | null;
}) {
  const cur = currentStage(j);
  const next = nextUp(j, now);
  const lastDone = [...j.stages].reverse().find((s) => s.status === "done");
  return (
    <article
      className={`${first ? "mt-4" : "mt-[14px]"} rounded-[18px] border px-[22px] py-5`}
      style={CARD}
    >
      <div className="flex flex-wrap items-center gap-[13px]">
        <CompanyTile initial={initial(j.company)} hue={tileHue(j.company)} />
        <RowTitle role={j.role} company={j.company} />
        <SourcePill source={j.source} />
        {next && withinWeek(next.at, now) && <Pill tone="good">{weekdayTime(next.at)}</Pill>}
      </div>
      <div className="mt-[18px] overflow-x-auto">
        <JourneyTrack stages={stagesToTrack(j, now)} layout="fixed" />
      </div>
      <div
        className="mt-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-[11.5px] font-medium"
        style={{ color: "#a3927f" }}
      >
        <div className="flex flex-wrap items-center gap-3">
          {cur ? (
            <button
              className="font-semibold"
              style={{ color: "var(--terracotta-d)" }}
              onClick={() => onSave({ ...j, stages: advanceStages(j.stages, cur.id) })}
            >
              ✓ Done with {cur.name.toLowerCase()}
            </button>
          ) : (
            <span>waiting to hear back</span>
          )}
          <span>·</span>
          <AddStageChip
            onAdd={(name) =>
              onSave({ ...j, stages: [...j.stages, newStage(name, cur ? "upcoming" : "current")] })
            }
          />
        </div>
        <div className="flex items-center gap-3.5">
          <button
            className="font-semibold"
            style={{ color: "var(--sage)" }}
            onClick={() => onSave({ ...j, outcome: "offer" })}
          >
            Offer 🎉
          </button>
          <button
            className="font-semibold"
            style={{ color: "var(--brick)" }}
            onClick={() =>
              onSave({
                ...j,
                outcome: "rejected",
                ended_at_stage_id: cur?.id ?? lastDone?.id ?? null,
              })
            }
          >
            Didn&apos;t move on
          </button>
          <button
            className="font-semibold"
            style={{ color: "#a3927f" }}
            onClick={() => onSave({ ...j, outcome: "withdrawn" })}
          >
            Withdrew
          </button>
        </div>
      </div>
      {error && (
        <p className="mt-2 text-[11.5px]" style={{ color: "var(--brick)" }}>
          {`Couldn't save that — ${error}`}
        </p>
      )}
    </article>
  );
}

/** A sent application no journey tracks yet: one Applied node + "Track this". */
function AppliedRow({
  app,
  first,
  onTrack,
}: {
  app: Application;
  first: boolean;
  onTrack: () => void;
}) {
  const input = applicationToJourneyInput(app, new Date().toISOString());
  const at = input.stages[0].scheduled_at ?? new Date().toISOString();
  return (
    <article
      className={`${first ? "mt-4" : "mt-[14px]"} rounded-[18px] border px-[22px] py-5`}
      style={CARD}
    >
      <div className="flex flex-wrap items-center gap-[13px]">
        <CompanyTile initial={initial(input.company)} hue={tileHue(input.company)} />
        <RowTitle role={input.role} company={input.company} />
        <SourcePill source="hermes" />
      </div>
      <div className="mt-[18px] overflow-x-auto">
        <JourneyTrack
          stages={[{ id: "applied", name: "Applied", status: "done", note: shortDate(at), noteTone: "muted" }]}
          layout="fixed"
        />
      </div>
      <div
        className="mt-3 flex flex-wrap items-center justify-between gap-2 text-[11.5px] font-medium"
        style={{ color: "#a3927f" }}
      >
        <span>applied {shortDate(at)} · not tracked yet</span>
        <button className="font-semibold" style={{ color: "var(--terracotta-d)" }} onClick={onTrack}>
          Track this →
        </button>
      </div>
    </article>
  );
}

function OfferRow({ journey: j }: { journey: Journey }) {
  return (
    <article
      className="mt-[14px] flex flex-wrap items-center gap-[13px] rounded-[18px] border px-[22px] py-4"
      style={{ background: "#f6faf3", borderColor: "#cfe0c8" }}
    >
      <CompanyTile initial={initial(j.company)} hue={tileHue(j.company)} />
      <RowTitle role={j.role} company={j.company} size={15} />
      <span className="text-[12.5px]" style={{ color: "var(--sage)" }}>
        all {j.stages.length} stages done
      </span>
      <span
        className="rounded-full px-[13px] py-[5px] text-[11.5px] font-bold tracking-[0.06em]"
        style={{ background: "var(--sage)", color: "#f6faf3" }}
      >
        OFFER 🎉
      </span>
    </article>
  );
}

function ClosedRow({ journey: j, onRemove }: { journey: Journey; onRemove: () => void }) {
  const endedName = j.stages.find((s) => s.id === j.ended_at_stage_id)?.name;
  return (
    <article
      className="mt-[14px] flex flex-wrap items-center gap-[13px] rounded-[18px] border px-[22px] py-4"
      style={CARD}
    >
      <CompanyTile initial={initial(j.company)} hue={tileHue(j.company)} />
      <RowTitle role={j.role} company={j.company} size={15} />
      <span className="text-[12.5px]" style={{ color: "#a3927f" }}>
        ended at {endedName ?? "—"}
      </span>
      <Pill tone="muted">{j.outcome === "rejected" ? "Didn't move on" : "Withdrew"}</Pill>
      <button className="wm-nav-quiet text-[11.5px]" onClick={onRemove}>
        Remove
      </button>
    </article>
  );
}

/** "+ Add a stage" → inline input (the old journal's chip, restyled to the row). */
function AddStageChip({ onAdd }: { onAdd: (name: string) => void }) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");

  if (!adding) {
    return (
      <button
        onClick={() => setAdding(true)}
        className="inline-flex items-center gap-1 rounded-[7px] px-[11px] py-1 font-semibold"
        style={{
          background: "var(--surface-warm)",
          border: "1px dashed #d9c4a8",
          color: "var(--terracotta-d)",
        }}
      >
        + Add a stage
      </button>
    );
  }
  const commit = () => {
    if (draft.trim()) onAdd(draft.trim());
    setDraft("");
    setAdding(false);
  };
  return (
    <input
      autoFocus
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") setAdding(false);
      }}
      onBlur={commit}
      placeholder="stage — e.g. Recruiter call"
      className="wm-input h-[26px] w-48 rounded-[7px] px-2 text-xs outline-none"
    />
  );
}

/** The inline "+ Add an opportunity" form under the header. */
function AddOpportunity({
  busy,
  onAdd,
  onClose,
}: {
  busy: boolean;
  onAdd: (company: string, role: string) => void;
  onClose: () => void;
}) {
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const ready = !!company.trim() && !!role.trim();

  function commit() {
    if (!ready) return;
    onAdd(company.trim(), role.trim());
  }
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") commit();
    if (e.key === "Escape") onClose();
  };

  return (
    <div
      className="mt-4 flex flex-wrap items-center gap-2.5 rounded-[14px] px-5 py-4"
      style={{
        border: "1px dashed #d9c4a8",
        background: "color-mix(in srgb, var(--surface-warm) 60%, transparent)",
      }}
    >
      <span className="text-[11px] font-semibold uppercase tracking-[0.05em]" style={{ color: "#a3927f" }}>
        Add an opportunity
      </span>
      <input
        autoFocus
        value={role}
        onChange={(e) => setRole(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Role"
        className="wm-input h-[34px] min-w-0 flex-1 rounded-[10px] px-2.5 text-[13px] outline-none"
      />
      <input
        value={company}
        onChange={(e) => setCompany(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Company"
        className="wm-input h-[34px] w-40 rounded-[10px] px-2.5 text-[13px] outline-none"
      />
      <button
        onClick={commit}
        disabled={!ready || busy}
        className="wm-cta h-[34px] rounded-[10px] px-3.5 text-[12.5px] font-semibold disabled:opacity-40"
      >
        + Add
      </button>
      <button onClick={onClose} className="wm-nav-quiet text-[12px]">
        Cancel
      </button>
    </div>
  );
}
