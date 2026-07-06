// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { saveMinScore, useMinScore } from "@/lib/session";
import type {
  DiscoverySettings,
  DiscoverySettingsResponse,
  Profile,
  ProfileResponse,
  RemoteStyle,
} from "@/lib/types";
import { avatarColor, initial, resolveUserAvatar } from "@/lib/ui";
import {
  ChipEditor,
  Divider,
  InlineText,
  MonoLabel,
  PencilBtn,
} from "@/components/editable";
import { TopNav } from "@/components/TopNav";

const REMOTE_OPTIONS: RemoteStyle[] = ["remote", "hybrid", "onsite"];

/**
 * Profile (mock 09): opened from the avatar. Identity band + two-column grid —
 * resume & match preferences left, summary / skills / experience right, all
 * editable in place. Every edit patches profiles/{uid} (autosaved) and re-runs
 * Matching on the next pass.
 */
export default function ProfilePage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [draft, setDraft] = useState<Profile | null>(null);
  const minScore = useMinScore();

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

  const { data, isLoading, error } = useQuery({
    queryKey: ["profile-page"],
    queryFn: () => apiFetch<ProfileResponse>("/profile"),
    enabled: !!user,
  });

  // Seed the editable draft once (render-time setState, guarded to run once).
  if (data?.profile && draft === null) setDraft(data.profile);

  useEffect(() => {
    if (data && data.profile === null) router.push("/onboarding");
  }, [data, router]);

  const save = useMutation({
    mutationFn: (profile: Profile) =>
      apiFetch("/profile", { method: "PUT", body: JSON.stringify(profile) }),
  });

  // Autosave: edits patch the profile doc after a short quiet period.
  const mutateSave = save.mutate;
  const lastSaved = useRef<string | null>(null);
  useEffect(() => {
    if (!draft) return;
    const json = JSON.stringify(draft);
    if (lastSaved.current === null) {
      lastSaved.current = json; // the freshly-loaded profile isn't an edit
      return;
    }
    if (lastSaved.current === json) return;
    const t = setTimeout(() => {
      lastSaved.current = json;
      mutateSave(draft);
    }, 800);
    return () => clearTimeout(t);
  }, [draft, mutateSave]);

  if (loading || !user || isLoading || !draft) {
    return (
      <>
        <TopNav section="profile" />
        <main className="p-8" style={{ color: "var(--muted)" }}>
          Loading…
        </main>
      </>
    );
  }
  if (error) {
    return (
      <>
        <TopNav section="profile" />
        <main className="p-8" style={{ color: "var(--danger)" }}>
          Failed to load your profile: {String(error)}
        </main>
      </>
    );
  }

  const email = draft.email || user.email || "";
  const headline = draft.preferences.target_titles?.[0] ?? "";
  const av = resolveUserAvatar(draft.full_name, email);
  const skills = Object.values(draft.skills ?? {}).flat();

  function patch(next: Partial<Profile>) {
    setDraft((d) => (d ? { ...d, ...next } : d));
  }

  function toggleRemote(style: RemoteStyle) {
    const cur = draft!.preferences.remote_policy ?? [];
    const next = cur.includes(style)
      ? cur.filter((s) => s !== style)
      : [...cur, style];
    patch({ preferences: { ...draft!.preferences, remote_policy: next } });
  }

  function removeSkill(label: string) {
    const next: Record<string, string[]> = {};
    for (const [cat, items] of Object.entries(draft!.skills ?? {})) {
      const kept = items.filter((s) => s !== label);
      if (kept.length) next[cat] = kept;
    }
    patch({ skills: next });
  }

  function addSkill(label: string) {
    const next = { ...(draft!.skills ?? {}) };
    const cat = Object.keys(next)[0] ?? "skills";
    next[cat] = [...(next[cat] ?? []), label];
    patch({ skills: next });
  }

  function setTargetTitles(titles: string[]) {
    patch({ preferences: { ...draft!.preferences, target_titles: titles } });
  }

  return (
    <>
      <TopNav section="profile" />
      <main className="mx-auto w-full max-w-[940px] flex-1 px-8 py-[26px]">
        {/* Identity band */}
        <div className="mb-5 flex items-center gap-4">
          <span
            className="flex h-14 w-14 flex-none items-center justify-center rounded-full text-xl font-bold"
            style={{ background: "var(--text)", color: "var(--surface)" }}
          >
            {av.kind === "glyph" ? "•" : av.text}
          </span>
          <div className="min-w-0 flex-1">
            <InlineText
              value={draft.full_name}
              textClass="text-[21px] font-semibold tracking-tight"
              placeholder="Your name"
              onSave={(v) => patch({ full_name: v })}
            />
            <div
              className="mt-1 font-mono text-[12.5px] font-medium"
              style={{ color: "var(--muted)" }}
            >
              {[headline, draft.location, email].filter(Boolean).join(" · ")}
            </div>
          </div>
          <div className="flex flex-none items-center gap-[7px]">
            {REMOTE_OPTIONS.map((style) => {
              const on = (draft.preferences.remote_policy ?? []).includes(style);
              return (
                <button
                  key={style}
                  onClick={() => toggleRemote(style)}
                  className="inline-flex items-center gap-1 rounded-full border px-2.5 py-[3px] font-mono text-[11px] font-semibold"
                  style={
                    on
                      ? {
                          background: "var(--good-bg)",
                          borderColor: "var(--good-border)",
                          color: "var(--good)",
                        }
                      : {
                          background: "var(--surface-2)",
                          borderColor: "var(--border)",
                          color: "var(--subtle)",
                        }
                  }
                >
                  {style}
                  {on && " ✓"}
                </button>
              );
            })}
          </div>
        </div>

        <div className="grid items-start gap-4 md:grid-cols-[320px_1fr]">
          {/* Left column */}
          <div className="flex flex-col gap-4">
            <Card>
              <MonoLabel>Resume</MonoLabel>
              <div className="mt-3 flex items-center gap-[11px]">
                <span
                  className="flex h-9 w-9 flex-none items-center justify-center rounded-lg border font-mono text-[10px] font-bold"
                  style={{
                    background: "var(--danger-bg)",
                    borderColor: "var(--danger-border)",
                    color: "var(--danger)",
                  }}
                >
                  PDF
                </span>
                <div className="min-w-0 flex-1">
                  <div
                    className="text-[13.5px] font-semibold"
                    style={{ color: "var(--text)" }}
                  >
                    Résumé on file
                  </div>
                  <div
                    className="mt-0.5 font-mono text-[11px] font-medium"
                    style={{ color: "var(--subtle)" }}
                  >
                    source of this profile
                  </div>
                </div>
              </div>
              <div className="mt-3 flex gap-2">
                <Link
                  href="/onboarding"
                  className="flex h-[34px] flex-1 items-center justify-center rounded-lg text-[12.5px] font-semibold"
                  style={{ background: "var(--text)", color: "var(--surface)" }}
                >
                  Replace
                </Link>
              </div>
              <Divider my={14} />
              <p
                className="font-mono text-[11px] font-medium leading-relaxed"
                style={{ color: "var(--subtle)" }}
              >
                re-uploading re-parses your profile — versions are kept, never
                overwritten
              </p>
            </Card>

            <Card>
              <MonoLabel>Match preferences</MonoLabel>
              <div className="mt-3 flex items-center gap-2.5">
                <span
                  className="flex-1 text-[13px] font-medium"
                  style={{ color: "var(--label)" }}
                >
                  Minimum score
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={minScore}
                  onChange={(e) => saveMinScore(Number(e.target.value))}
                  className="w-[110px] accent-[var(--accent)]"
                />
                <span
                  className="w-5 text-right font-mono text-[13px] font-semibold tabular-nums"
                  style={{ color: "var(--text)" }}
                >
                  {minScore}
                </span>
              </div>
              <Divider my={13} />
              <MonoLabel>Target titles</MonoLabel>
              <div className="mt-2">
                <ChipEditor
                  items={draft.preferences.target_titles ?? []}
                  onRemove={(t) =>
                    setTargetTitles(
                      (draft.preferences.target_titles ?? []).filter(
                        (x) => x !== t,
                      ),
                    )
                  }
                  onAdd={(t) =>
                    setTargetTitles([
                      ...(draft.preferences.target_titles ?? []),
                      t,
                    ])
                  }
                  addLabel="+ Add"
                />
              </div>
            </Card>

            <AutoDiscoveryCard />
          </div>

          {/* Right column */}
          <div className="flex flex-col gap-4">
            <Card>
              <SummaryBlock
                value={draft.objective_template}
                onSave={(v) => patch({ objective_template: v })}
              />
            </Card>

            <Card>
              <div className="flex items-center justify-between">
                <MonoLabel>Skills · {skills.length}</MonoLabel>
                <span
                  className="font-mono text-[11px] font-medium"
                  style={{ color: "var(--subtle)" }}
                >
                  × to remove
                </span>
              </div>
              <div className="mt-2.5">
                <ChipEditor
                  items={skills}
                  onRemove={removeSkill}
                  onAdd={addSkill}
                />
              </div>
            </Card>

            <Card>
              <MonoLabel>
                Experience · {draft.experience.length} roles
              </MonoLabel>
              <ExperienceList
                experience={draft.experience}
                onChange={(experience) => patch({ experience })}
              />
            </Card>
          </div>
        </div>

        <p
          className="mt-5 text-center font-mono text-[11px] font-medium"
          style={{ color: "var(--subtle)" }}
        >
          edits patch profiles/{"{uid}"} · any change re-runs matching ·{" "}
          {save.isPending
            ? "saving…"
            : save.isError
              ? `save failed: ${String(save.error)}`
              : save.isSuccess
                ? "saved ✓"
                : "autosaves as you edit"}
        </p>
      </main>
    </>
  );
}

const INTERVALS: { hours: number; label: string }[] = [
  { hours: 6, label: "6h" },
  { hours: 12, label: "12h" },
  { hours: 24, label: "24h" },
  { hours: 72, label: "3d" },
];

function relPast(iso?: string | null): string {
  if (!iso) return "never";
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 48 * 60) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / (24 * 60))}d ago`;
}

function relNext(iso?: string | null): string {
  if (!iso) return "on next visit";
  const mins = Math.floor((new Date(iso).getTime() - Date.now()) / 60_000);
  if (mins <= 0) return "due now";
  if (mins < 60) return `in ${mins}m`;
  if (mins < 48 * 60) return `in ${Math.floor(mins / 60)}h`;
  return `in ${Math.floor(mins / (24 * 60))}d`;
}

/**
 * Auto-discovery (user request on top of mock 09): the agents' unattended
 * cadence, regulated from the profile. Two opt-in loops — discover+score new
 * jobs, and the liveness sweep that dismisses postings their ATS took down
 * (so the queue, shelves, and tracking never serve a dead posting).
 */
function AutoDiscoveryCard() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["discovery-settings"],
    queryFn: () => apiFetch<DiscoverySettingsResponse>("/settings/discovery"),
    // Runs finish in the background — keep the status line fresh.
    refetchInterval: 30_000,
  });

  const save = useMutation({
    mutationFn: (s: DiscoverySettings) =>
      apiFetch("/settings/discovery", { method: "PUT", body: JSON.stringify(s) }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["discovery-settings"] }),
  });
  const trigger = useMutation({
    mutationFn: (kind: "run" | "sweep") =>
      apiFetch(`/settings/discovery/${kind}`, { method: "POST" }),
    onSuccess: () =>
      setTimeout(
        () =>
          queryClient.invalidateQueries({ queryKey: ["discovery-settings"] }),
        5000,
      ),
  });

  if (!data) {
    return (
      <Card>
        <MonoLabel>Auto-discovery</MonoLabel>
        <p className="mt-3 text-[13px]" style={{ color: "var(--subtle)" }}>
          Loading…
        </p>
      </Card>
    );
  }

  const s = data.settings;
  const patch = (next: Partial<DiscoverySettings>) => save.mutate({ ...s, ...next });
  const sweep = data.state.last_sweep;

  return (
    <Card>
      <MonoLabel>Auto-discovery</MonoLabel>

      <div className="mt-3 flex items-center gap-2.5">
        <span
          className="flex-1 text-[13px] font-medium"
          style={{ color: "var(--label)" }}
        >
          Find new jobs
        </span>
        <Toggle
          on={s.auto_discovery}
          onClick={() => patch({ auto_discovery: !s.auto_discovery })}
        />
      </div>
      {s.auto_discovery && (
        <IntervalChips
          value={s.discovery_interval_hours}
          onChange={(h) => patch({ discovery_interval_hours: h })}
        />
      )}
      <div
        className="mt-2 font-mono text-[11px] font-medium"
        style={{ color: "var(--subtle)" }}
      >
        {s.auto_discovery
          ? `last ${relPast(data.state.last_discovery_at)} · next ${relNext(data.next_discovery_at)}`
          : "off — run from the CLI or the button below"}
      </div>

      <Divider my={13} />

      <div className="flex items-center gap-2.5">
        <span
          className="flex-1 text-[13px] font-medium"
          style={{ color: "var(--label)" }}
        >
          Invalidate stale postings
        </span>
        <Toggle
          on={s.liveness_sweep}
          onClick={() => patch({ liveness_sweep: !s.liveness_sweep })}
        />
      </div>
      {s.liveness_sweep && (
        <IntervalChips
          value={s.sweep_interval_hours}
          onChange={(h) => patch({ sweep_interval_hours: h })}
        />
      )}
      <div
        className="mt-2 font-mono text-[11px] font-medium"
        style={{ color: "var(--subtle)" }}
      >
        {s.liveness_sweep
          ? `last ${relPast(data.state.last_sweep_at)} · next ${relNext(data.next_sweep_at)}`
          : "off — taken-down postings stay until acted on"}
        {sweep && ` · ${sweep.removed} removed of ${sweep.checked} checked`}
      </div>

      <Divider my={13} />

      <div className="flex gap-2">
        <button
          onClick={() => trigger.mutate("run")}
          disabled={trigger.isPending}
          className="h-[30px] flex-1 rounded-[7px] border text-xs font-semibold"
          style={{
            background: "var(--surface)",
            borderColor: "var(--border)",
            color: "var(--label)",
          }}
        >
          Run discovery now
        </button>
        <button
          onClick={() => trigger.mutate("sweep")}
          disabled={trigger.isPending}
          className="h-[30px] flex-1 rounded-[7px] border text-xs font-semibold"
          style={{
            background: "var(--surface)",
            borderColor: "var(--border)",
            color: "var(--label)",
          }}
        >
          Sweep now
        </button>
      </div>
      {trigger.isSuccess && (
        <p
          className="mt-2 font-mono text-[11px] font-medium"
          style={{ color: "var(--good)" }}
        >
          started — results land here as the agent finishes
        </p>
      )}
    </Card>
  );
}

function Toggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-pressed={on}
      className="relative h-5 w-9 flex-none rounded-full transition-colors"
      style={{ background: on ? "var(--accent)" : "var(--border-mid)" }}
    >
      <span
        className="absolute top-[2px] h-4 w-4 rounded-full transition-all"
        style={{ left: on ? 18 : 2, background: "var(--surface)" }}
      />
    </button>
  );
}

function IntervalChips({
  value,
  onChange,
}: {
  value: number;
  onChange: (hours: number) => void;
}) {
  return (
    <div className="mt-2 flex gap-1.5">
      {INTERVALS.map(({ hours, label }) => {
        const active = hours === value;
        return (
          <button
            key={hours}
            onClick={() => onChange(hours)}
            className="rounded-[7px] border px-2.5 py-1 font-mono text-[11px] font-semibold"
            style={
              active
                ? {
                    background: "var(--text)",
                    borderColor: "var(--text)",
                    color: "var(--surface)",
                  }
                : {
                    background: "var(--surface-2)",
                    borderColor: "var(--border)",
                    color: "var(--subtle)",
                  }
            }
          >
            every {label}
          </button>
        );
      })}
    </div>
  );
}

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="rounded-[14px] border px-[18px] py-4"
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      {children}
    </div>
  );
}

/** "What Matching reads" — the generated candidate summary, edited in place. */
function SummaryBlock({
  value,
  onSave,
}: {
  value: string;
  onSave: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(value);

  if (editing) {
    return (
      <div>
        <MonoLabel>Summary — what Matching reads</MonoLabel>
        <textarea
          autoFocus
          value={text}
          rows={4}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSave(text.trim());
              setEditing(false);
            }
            if (e.key === "Escape") setEditing(false);
          }}
          className="mt-2 w-full rounded-lg p-[11px] text-[13.5px] leading-relaxed outline-none"
          style={{
            background: "var(--surface)",
            border: "2px solid var(--accent)",
            color: "var(--text)",
            boxShadow:
              "0 0 0 3px color-mix(in srgb, var(--accent) 13%, transparent)",
          }}
        />
        <div
          className="mt-1.5 font-mono text-[11px] font-medium"
          style={{ color: "var(--subtle)" }}
        >
          <span style={{ color: "var(--accent)" }}>↵ save</span> · esc cancel
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-3">
      <div className="flex-1">
        <MonoLabel>Summary — what Matching reads</MonoLabel>
        <p
          className="mt-2 text-[13.5px] leading-relaxed"
          style={{ color: "var(--label)" }}
        >
          {value || (
            <span style={{ color: "var(--subtle)" }}>
              No summary yet — add the paragraph Matching should read.
            </span>
          )}
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

function ExperienceList({
  experience,
  onChange,
}: {
  experience: Profile["experience"];
  onChange: (next: Profile["experience"]) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? experience : experience.slice(0, 2);
  const hidden = experience.length - visible.length;

  return (
    <div>
      {visible.map((role, i) => (
        <div key={i}>
          {i > 0 && <Divider my={12} />}
          <ExperienceItem
            role={role}
            expanded={i === 0}
            onChange={(r) =>
              onChange(experience.map((e, j) => (j === i ? r : e)))
            }
          />
        </div>
      ))}
      {hidden > 0 && (
        <button
          onClick={() => setShowAll(true)}
          className="ml-[38px] mt-2.5 text-xs font-medium"
          style={{ color: "var(--muted)" }}
        >
          Show {hidden} more…
        </button>
      )}
      {showAll && experience.length > 2 && (
        <button
          onClick={() => setShowAll(false)}
          className="ml-[38px] mt-2.5 text-xs font-medium"
          style={{ color: "var(--muted)" }}
        >
          Show less
        </button>
      )}
    </div>
  );
}

function ExperienceItem({
  role,
  expanded,
  onChange,
}: {
  role: Profile["experience"][number];
  expanded: boolean;
  onChange: (r: Profile["experience"][number]) => void;
}) {
  const [editing, setEditing] = useState(false);
  const av = avatarColor(role.company);
  const years = `${fmtYear(role.start)}–${role.end ? fmtYear(role.end) : "Present"}`;

  return (
    <div className="mt-3 flex items-start gap-2.5 first:mt-0">
      <span
        className="flex h-7 w-7 flex-none items-center justify-center rounded-[7px] text-xs font-bold"
        style={{ background: av.bg, color: av.color }}
      >
        {initial(role.company)}
      </span>
      <div className="min-w-0 flex-1">
        {editing ? (
          <div className="flex flex-col gap-2">
            <div className="flex gap-2">
              <EditInput
                value={role.role}
                placeholder="Role"
                onCommit={(v) => onChange({ ...role, role: v })}
              />
              <EditInput
                value={role.company}
                placeholder="Company"
                onCommit={(v) => onChange({ ...role, company: v })}
              />
            </div>
            {role.bullets.map((b, i) => (
              <textarea
                key={i}
                value={b.text}
                rows={2}
                onChange={(e) =>
                  onChange({
                    ...role,
                    bullets: role.bullets.map((x, j) =>
                      j === i ? { ...x, text: e.target.value } : x,
                    ),
                  })
                }
                className="w-full rounded-lg border p-2 text-[12.5px] leading-relaxed outline-none"
                style={{
                  background: "var(--surface)",
                  borderColor: "var(--border)",
                  color: "var(--label)",
                }}
              />
            ))}
          </div>
        ) : (
          <>
            <div className="text-[13.5px]" style={{ color: "var(--muted)" }}>
              <b style={{ color: "var(--text)" }}>{role.role}</b> · {role.company}{" "}
              · {years}
            </div>
            {expanded && role.bullets.length > 0 && (
              <ul className="mt-[7px] flex list-disc flex-col gap-1 pl-4">
                {role.bullets.map((b, i) => (
                  <li
                    key={i}
                    className="text-[12.5px] leading-normal"
                    style={{ color: "var(--muted)" }}
                  >
                    {b.text}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
      <PencilBtn onClick={() => setEditing((e) => !e)} />
    </div>
  );
}

function EditInput({
  value,
  placeholder,
  onCommit,
}: {
  value: string;
  placeholder: string;
  onCommit: (v: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  return (
    <input
      value={draft}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => draft.trim() && draft !== value && onCommit(draft.trim())}
      onKeyDown={(e) => {
        if (e.key === "Enter" && draft.trim()) onCommit(draft.trim());
      }}
      className="h-8 min-w-0 flex-1 rounded-lg px-2 text-[13px] outline-none"
      style={{
        background: "var(--surface)",
        border: "2px solid var(--accent)",
        color: "var(--text)",
        boxShadow: "0 0 0 3px color-mix(in srgb, var(--accent) 13%, transparent)",
      }}
    />
  );
}

function fmtYear(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : String(d.getFullYear());
}
