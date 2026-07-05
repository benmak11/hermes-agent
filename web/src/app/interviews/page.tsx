// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import {
  advanceStages,
  currentStage,
  newEntry,
  saveEntries,
  stagePosition,
  useJournal,
  type JournalEntry,
  type Stage,
} from "@/lib/interviews";
import type { Application } from "@/lib/types";
import { avatarColor, initial } from "@/lib/ui";
import { MonoLabel, PencilBtn } from "@/components/editable";
import { TopNav } from "@/components/TopNav";

type ListResponse = { applications: Application[] };

/**
 * Interview journal (mock 08): user-logged, never auto-tracked. Hermes only
 * keeps the score line; stages, outcomes, and reflections belong to the user.
 */
export default function InterviewsPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const entries = useJournal(user?.uid ?? null);

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

  // "applied" comes from real submissions; the journal never invents it.
  const { data: appsData } = useQuery({
    queryKey: ["applications"],
    queryFn: () => apiFetch<ListResponse>("/applications"),
    enabled: !!user,
  });

  if (loading || !user) {
    return (
      <>
        <TopNav section="interviews" />
        <main className="p-8" style={{ color: "var(--muted)" }}>
          Loading…
        </main>
      </>
    );
  }

  const applied = (appsData?.applications ?? []).filter(
    (a) => a.status === "submitted" || a.status === "responded",
  ).length;
  const landed = entries.length;
  const inProgress = entries.filter((e) => e.outcome === "in_progress").length;
  const offers = entries.filter((e) => e.outcome === "offer").length;

  function update(next: JournalEntry[]) {
    saveEntries(user!.uid, next);
  }

  function patchEntry(id: string, patch: Partial<JournalEntry>) {
    update(entries.map((e) => (e.id === id ? { ...e, ...patch } : e)));
  }

  const ordered = [...entries].sort((a, b) => {
    const rank = (e: JournalEntry) =>
      e.outcome === "offer" ? 0 : e.outcome === "in_progress" ? 1 : 2;
    return rank(a) - rank(b) || b.createdAt - a.createdAt;
  });

  return (
    <>
      <TopNav section="interviews" />
      <main className="mx-auto w-full max-w-[820px] flex-1 px-8 py-[26px]">
        <div className="mb-[18px] flex items-end justify-between">
          <div>
            <h1
              className="text-[22px] font-semibold tracking-tight"
              style={{ color: "var(--text)" }}
            >
              Interviews
            </h1>
            <div
              className="mt-2 font-mono text-[12.5px] font-medium"
              style={{ color: "var(--muted)" }}
            >
              <b style={{ color: "var(--text)" }}>{applied}</b> applied ·{" "}
              <b style={{ color: "var(--accent-text)" }}>{landed}</b> interviews
              landed · <b style={{ color: "var(--text)" }}>{inProgress}</b> in
              progress · <b style={{ color: "var(--good)" }}>{offers}</b>{" "}
              {offers === 1 ? "offer" : "offers"}
            </div>
          </div>
          <span className="text-xs font-medium" style={{ color: "var(--subtle)" }}>
            logged by you — Hermes just keeps the score
          </span>
        </div>

        {ordered.length === 0 && (
          <div
            className="rounded-[14px] border px-6 py-8 text-center"
            style={{ background: "var(--surface)", borderColor: "var(--border)" }}
          >
            <p className="text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
              Nothing logged yet. When an application turns into a recruiter
              call, log it below — stages, notes, and outcomes stay yours.
            </p>
          </div>
        )}

        <div className="flex flex-col gap-3.5">
          {ordered.map((entry) =>
            entry.outcome === "offer" ? (
              <OfferCard
                key={entry.id}
                entry={entry}
                onPatch={(p) => patchEntry(entry.id, p)}
              />
            ) : entry.outcome === "rejected" ? (
              <RejectedCard
                key={entry.id}
                entry={entry}
                onPatch={(p) => patchEntry(entry.id, p)}
              />
            ) : (
              <InProgressCard
                key={entry.id}
                entry={entry}
                onPatch={(p) => patchEntry(entry.id, p)}
              />
            ),
          )}
        </div>

        <AddEntry onAdd={(company, role) => update([...entries, newEntry(company, role)])} />

        <p className="mt-5 text-center">
          <Link
            href="/tracking"
            className="font-mono text-[11px] font-medium"
            style={{ color: "var(--subtle)" }}
          >
            application submissions ↗
          </Link>
        </p>
      </main>
    </>
  );
}

function CardHeader({
  entry,
  pill,
}: {
  entry: JournalEntry;
  pill: React.ReactNode;
}) {
  const av = avatarColor(entry.company);
  return (
    <div className="flex items-center gap-3">
      <span
        className="flex h-[34px] w-[34px] flex-none items-center justify-center rounded-lg text-[15px] font-bold"
        style={{ background: av.bg, color: av.color }}
      >
        {initial(entry.company)}
      </span>
      <div className="min-w-0 flex-1">
        <span
          className="text-[15.5px] font-semibold"
          style={{ color: "var(--text)" }}
        >
          {entry.role}
        </span>
        <span className="text-[13px]" style={{ color: "var(--muted)" }}>
          {" "}
          · {entry.company}
        </span>
      </div>
      {pill}
    </div>
  );
}

// ---- In progress ----

function InProgressCard({
  entry,
  onPatch,
}: {
  entry: JournalEntry;
  onPatch: (p: Partial<JournalEntry>) => void;
}) {
  const pos = stagePosition(entry);
  const cur = currentStage(entry);

  return (
    <div
      className="rounded-[14px] border px-5 py-[18px]"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <CardHeader
        entry={entry}
        pill={
          <span
            className="inline-flex items-center rounded-full border px-2.5 py-[3px] font-mono text-[11px] font-semibold"
            style={{
              background: "var(--accent-bg)",
              borderColor: "var(--accent-border)",
              color: "var(--accent-text)",
            }}
          >
            stage {pos.n} of {pos.of}
          </span>
        }
      />

      <div className="mt-3.5 flex flex-wrap items-center gap-[7px]">
        {entry.stages.map((s) => (
          <StageChip
            key={s.id}
            stage={s}
            onClick={() =>
              onPatch({ stages: advanceStages(entry.stages, s.id) })
            }
          />
        ))}
        <AddStageChip
          onAdd={(name) =>
            onPatch({
              stages: [
                ...entry.stages,
                { id: crypto.randomUUID(), name, status: "upcoming" },
              ],
            })
          }
        />
      </div>

      <div
        className="mt-3 flex items-center justify-between font-mono text-[11.5px] font-medium"
        style={{ color: "var(--subtle)" }}
      >
        <span>
          {cur ? (
            <>
              after {cur.name.toLowerCase()}:{" "}
              <span style={{ color: "var(--accent-text)" }}>
                ✎ what went well / what to improve
              </span>
            </>
          ) : (
            "all stages done — log the outcome"
          )}
        </span>
        <span className="flex gap-3">
          <button
            onClick={() => onPatch({ outcome: "offer" })}
            className="font-semibold"
            style={{ color: "var(--good)" }}
          >
            offer 🎉
          </button>
          <button
            onClick={() =>
              onPatch({ outcome: "rejected", endedAtStage: cur?.name })
            }
            className="font-semibold"
            style={{ color: "var(--danger)" }}
          >
            rejected
          </button>
        </span>
      </div>
    </div>
  );
}

function StageChip({ stage, onClick }: { stage: Stage; onClick: () => void }) {
  if (stage.status === "done") {
    return (
      <button
        onClick={onClick}
        className="inline-flex items-center gap-1.5 rounded-[7px] border px-[11px] py-1 text-xs font-semibold"
        style={{
          background: "var(--good-bg)",
          borderColor: "var(--good-border)",
          color: "var(--good)",
        }}
      >
        ✓ {stage.name}
      </button>
    );
  }
  if (stage.status === "current") {
    return (
      <button
        onClick={onClick}
        className="inline-flex items-center gap-1.5 rounded-[7px] px-[11px] py-1 text-xs font-semibold"
        style={{
          background: "var(--accent-bg)",
          border: "2px solid var(--accent)",
          color: "var(--accent-text)",
        }}
      >
        ● {stage.name}
      </button>
    );
  }
  return (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-[7px] border px-[11px] py-1 text-xs font-semibold"
      style={{
        background: "var(--surface-2)",
        borderColor: "var(--border)",
        color: "var(--subtle)",
      }}
    >
      {stage.name}
    </button>
  );
}

function AddStageChip({ onAdd }: { onAdd: (name: string) => void }) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");

  if (!adding) {
    return (
      <button
        onClick={() => setAdding(true)}
        className="inline-flex items-center gap-1 rounded-[7px] px-[11px] py-1 text-xs font-semibold"
        style={{
          background: "var(--surface)",
          border: "1px dashed var(--border-mid)",
          color: "var(--accent)",
        }}
      >
        + Add stage
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
      placeholder="stage — e.g. Onsite · Thu 10am"
      className="h-[26px] w-48 rounded-[7px] px-2 text-xs outline-none"
      style={{
        background: "var(--surface)",
        border: "2px solid var(--accent)",
        color: "var(--text)",
        boxShadow: "0 0 0 3px color-mix(in srgb, var(--accent) 13%, transparent)",
      }}
    />
  );
}

// ---- Offer (celebration) ----

const CONFETTI: React.CSSProperties[] = [
  { top: 12, right: 150, width: 6, height: 11, borderRadius: 2, background: "var(--star)", transform: "rotate(24deg)", animationDelay: ".05s" },
  { top: 30, right: 112, width: 6, height: 6, borderRadius: "50%", background: "#60a5fa", animationDelay: ".15s" },
  { top: 10, right: 84, width: 6, height: 11, borderRadius: 2, background: "#4ade80", transform: "rotate(-18deg)", animationDelay: ".25s" },
  { top: 34, right: 52, width: 6, height: 11, borderRadius: 2, background: "#f472b6", transform: "rotate(40deg)", animationDelay: ".35s" },
  { top: 14, right: 26, width: 6, height: 6, borderRadius: "50%", background: "var(--star)", animationDelay: ".45s" },
];

function OfferCard({
  entry,
  onPatch,
}: {
  entry: JournalEntry;
  onPatch: (p: Partial<JournalEntry>) => void;
}) {
  const finalIdx = entry.stages.length - 1;

  return (
    <div
      className="relative overflow-hidden rounded-[14px] border px-5 py-[18px]"
      style={{ background: "var(--offer-bg)", borderColor: "var(--good-border)" }}
    >
      {CONFETTI.map((style, i) => (
        <span key={i} className="h-popin absolute" style={{ ...style, animationDuration: ".5s" }} />
      ))}

      <CardHeader
        entry={entry}
        pill={
          <span
            className="h-popin inline-flex items-center gap-1.5 rounded-full px-3 py-1 font-mono text-[11px] font-bold tracking-wide"
            style={{ background: "var(--good)", color: "var(--surface)" }}
          >
            OFFER 🎉
          </span>
        }
      />

      <div className="mt-3.5 flex flex-wrap items-center gap-[7px]">
        {entry.stages.map((s, i) =>
          i === finalIdx ? (
            <span
              key={s.id}
              className="inline-flex items-center gap-1.5 rounded-[7px] px-[11px] py-1 text-xs font-semibold"
              style={{
                background: "var(--good)",
                border: "1px solid var(--good)",
                color: "var(--surface)",
              }}
            >
              ✓ {s.name}
              {entry.sessions.length > 0 &&
                ` · ${entry.sessions.length} sessions`}
            </span>
          ) : (
            <span
              key={s.id}
              className="inline-flex items-center gap-1.5 rounded-[7px] border px-[11px] py-1 text-xs font-semibold"
              style={{
                background: "var(--good-bg)",
                borderColor: "var(--good-border)",
                color: "var(--good)",
              }}
            >
              ✓ {s.name}
            </span>
          ),
        )}
        {entry.sessions.length > 0 && (
          <span
            className="font-mono text-[11px] font-medium"
            style={{ color: "var(--subtle)" }}
          >
            →
          </span>
        )}
        {entry.sessions.map((s, i) => (
          <span
            key={i}
            className="rounded-md border px-2 py-[3px] font-mono text-[11px] font-medium"
            style={{
              background: "var(--surface)",
              borderColor: "var(--offer-divider)",
              color: "var(--label)",
            }}
          >
            {s}
          </span>
        ))}
        <SessionAdder
          onAdd={(name) => onPatch({ sessions: [...entry.sessions, name] })}
        />
      </div>

      <div className="my-3.5 h-px" style={{ background: "var(--offer-divider)" }} />

      <ReflectionBlock
        label="Reflection — what carried it"
        color="var(--good)"
        value={entry.reflection ?? ""}
        placeholder="What prep or stories made the difference?"
        onSave={(reflection) => onPatch({ reflection })}
      />
    </div>
  );
}

function SessionAdder({ onAdd }: { onAdd: (name: string) => void }) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  if (!adding) {
    return (
      <button
        onClick={() => setAdding(true)}
        className="rounded-md px-2 py-[3px] font-mono text-[11px] font-semibold"
        style={{
          background: "var(--surface)",
          border: "1px dashed var(--good-border)",
          color: "var(--good)",
        }}
      >
        + session
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
      placeholder="session…"
      className="h-[24px] w-36 rounded-md px-2 font-mono text-[11px] outline-none"
      style={{
        background: "var(--surface)",
        border: "2px solid var(--good)",
        color: "var(--text)",
      }}
    />
  );
}

// ---- Rejected (reflection) ----

function RejectedCard({
  entry,
  onPatch,
}: {
  entry: JournalEntry;
  onPatch: (p: Partial<JournalEntry>) => void;
}) {
  const endedIdx = Math.max(
    0,
    entry.stages.findIndex((s) => s.name === entry.endedAtStage),
  );
  const ended = entry.stages[endedIdx];

  function patchStageNotes(patch: Partial<Stage>) {
    onPatch({
      stages: entry.stages.map((s, i) =>
        i === endedIdx ? { ...s, ...patch } : s,
      ),
    });
  }

  return (
    <div
      className="rounded-[14px] border px-5 py-[18px]"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      <CardHeader
        entry={entry}
        pill={
          <span
            className="inline-flex items-center rounded-full border px-2.5 py-[3px] font-mono text-[11px] font-semibold"
            style={{
              background: "var(--danger-bg)",
              borderColor: "var(--danger-border)",
              color: "var(--danger)",
            }}
          >
            ended at {ended?.name.toLowerCase() ?? "—"} · stage {endedIdx + 1} of{" "}
            {entry.stages.length}
          </span>
        }
      />

      <div className="mt-3.5 grid gap-4 sm:grid-cols-2">
        <div
          className="rounded-[10px] border px-3.5 py-3"
          style={{
            background: "var(--good-panel-bg)",
            borderColor: "var(--good-panel-border)",
          }}
        >
          <ReflectionBlock
            label="What went well"
            color="var(--good)"
            value={ended?.wentWell ?? ""}
            placeholder="What worked in this process?"
            onSave={(wentWell) => patchStageNotes({ wentWell })}
          />
        </div>
        <div
          className="rounded-[10px] border px-3.5 py-3"
          style={{
            background: "var(--danger-panel-bg)",
            borderColor: "var(--danger-panel-border)",
          }}
        >
          <ReflectionBlock
            label="What to improve"
            color="var(--danger)"
            value={ended?.toImprove ?? ""}
            placeholder="What would you practice before the next one?"
            onSave={(toImprove) => patchStageNotes({ toImprove })}
          />
        </div>
      </div>

      <div
        className="mt-3 font-mono text-[11.5px] font-medium"
        style={{ color: "var(--subtle)" }}
      >
        notes saved per stage · visible next time a similar role comes up
      </div>
    </div>
  );
}

// ---- Shared reflection editor ----

function ReflectionBlock({
  label,
  color,
  value,
  placeholder,
  onSave,
}: {
  label: string;
  color: string;
  value: string;
  placeholder: string;
  onSave: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(value);

  if (editing) {
    return (
      <div>
        <MonoLabel color={color}>{label}</MonoLabel>
        <textarea
          autoFocus
          value={text}
          rows={3}
          placeholder={placeholder}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSave(text.trim());
              setEditing(false);
            }
            if (e.key === "Escape") setEditing(false);
          }}
          onBlur={() => {
            onSave(text.trim());
            setEditing(false);
          }}
          className="mt-1.5 w-full rounded-lg p-2 text-[13px] leading-relaxed outline-none"
          style={{
            background: "var(--surface)",
            border: "2px solid var(--accent)",
            color: "var(--text)",
            boxShadow:
              "0 0 0 3px color-mix(in srgb, var(--accent) 13%, transparent)",
          }}
        />
      </div>
    );
  }

  return (
    <div className="flex items-start gap-3">
      <div className="min-w-0 flex-1">
        <MonoLabel color={color}>{label}</MonoLabel>
        <p
          className="mt-1.5 text-[13px] leading-relaxed"
          style={{ color: value ? "var(--label)" : "var(--subtle)" }}
        >
          {value || placeholder}
        </p>
      </div>
      <PencilBtn
        onClick={() => {
          setText(value);
          setEditing(true);
        }}
      />
    </div>
  );
}

// ---- Add entry ----

function AddEntry({
  onAdd,
}: {
  onAdd: (company: string, role: string) => void;
}) {
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");

  function commit() {
    if (!company.trim() || !role.trim()) return;
    onAdd(company.trim(), role.trim());
    setCompany("");
    setRole("");
  }

  const inputStyle: React.CSSProperties = {
    background: "var(--surface)",
    border: "1px solid var(--border)",
    color: "var(--text)",
  };

  return (
    <div
      className="mt-3.5 flex flex-wrap items-center gap-2.5 rounded-[14px] px-5 py-4"
      style={{
        border: "1px dashed var(--border-mid)",
        background: "color-mix(in srgb, var(--surface) 60%, transparent)",
      }}
    >
      <span
        className="font-mono text-[11px] font-semibold uppercase"
        style={{ color: "var(--subtle)", letterSpacing: "0.05em" }}
      >
        Log interview
      </span>
      <input
        value={role}
        onChange={(e) => setRole(e.target.value)}
        placeholder="Role"
        className="h-[34px] min-w-0 flex-1 rounded-lg px-2.5 text-[13px] outline-none"
        style={inputStyle}
      />
      <input
        value={company}
        onChange={(e) => setCompany(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && commit()}
        placeholder="Company"
        className="h-[34px] w-40 rounded-lg px-2.5 text-[13px] outline-none"
        style={inputStyle}
      />
      <button
        onClick={commit}
        disabled={!company.trim() || !role.trim()}
        className="h-[34px] rounded-lg px-3.5 text-[12.5px] font-semibold disabled:opacity-40"
        style={{ background: "var(--text)", color: "var(--surface)" }}
      >
        + Add
      </button>
    </div>
  );
}
