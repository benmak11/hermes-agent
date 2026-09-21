// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { signOut } from "firebase/auth";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { auth } from "@/lib/firebase";
import { saveMinScore, useMinScore } from "@/lib/session";
import type {
  DiscoverySettings,
  DiscoverySettingsResponse,
  Profile,
  ProfileResponse,
  RemoteStyle,
} from "@/lib/types";
import { initial, resolveUserAvatar } from "@/lib/ui";
import { CompanyTile, tileHue } from "@/components/warm/CompanyTile";
import {
  ChipEditor,
  Divider,
  InlineText,
  MonoLabel,
  PencilBtn,
} from "@/components/warm/Editable";
import { SERIF } from "@/components/warm/styles";
import { TopNav } from "@/components/TopNav";

const REMOTE_OPTIONS: RemoteStyle[] = ["remote", "hybrid", "onsite"];

/**
 * Profile (Warm Flow 14): opened from the avatar. Identity band + two-column
 * grid — resume & match preferences left, summary / skills / experience right,
 * all editable in place. Every edit patches profiles/{uid} (autosaved) and is
 * read on the next discovery pass.
 */
export default function ProfilePage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [draft, setDraft] = useState<Profile | null>(null);
  const minScore = useMinScore();

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
        <main className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
          Loading…
        </main>
      </>
    );
  }
  if (error) {
    return (
      <>
        <TopNav section="profile" />
        <main className="p-8 text-[13.5px]" style={{ color: "var(--brick)" }}>
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
      <main className="mx-auto w-full max-w-[960px] flex-1 px-7 py-7">
        {/* Identity band */}
        <div className="mb-5 flex items-center gap-4">
          <span
            className="flex h-[58px] w-[58px] flex-none items-center justify-center rounded-full text-[21px] font-bold"
            style={{ background: "var(--terracotta)", color: "#fff9f2" }}
          >
            {av.kind === "glyph" ? "•" : av.text}
          </span>
          <div className="min-w-0 flex-1">
            <div style={{ fontFamily: SERIF }}>
              <InlineText
                value={draft.full_name}
                textClass="text-[28px] font-normal leading-[1.15]"
                placeholder="Your name"
                onSave={(v) => patch({ full_name: v })}
              />
            </div>
            <div className="mt-[5px] text-[13px]" style={{ color: "var(--ink-4)" }}>
              {[headline, draft.location, email].filter(Boolean).join(" · ")}
            </div>
          </div>
          <div className="flex flex-none items-center gap-2">
            {REMOTE_OPTIONS.map((style) => {
              const on = (draft.preferences.remote_policy ?? []).includes(style);
              return (
                <button
                  key={style}
                  onClick={() => toggleRemote(style)}
                  className="inline-flex items-center gap-1 rounded-full border px-3 py-[5px] text-[11.5px] font-bold"
                  style={
                    on
                      ? {
                          background: "var(--sage-tint)",
                          borderColor: "#cfe0c8",
                          color: "var(--sage)",
                        }
                      : {
                          background: "#f6ede1",
                          borderColor: "var(--border-warm-hair)",
                          color: "#a3927f",
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

        <div className="grid items-start gap-4 md:grid-cols-[330px_1fr]">
          {/* Left column */}
          <div className="flex flex-col gap-4">
            <Card>
              <MonoLabel>Your resume</MonoLabel>
              <div className="mt-3 flex items-center gap-3">
                <span
                  className="flex h-[38px] w-[38px] flex-none items-center justify-center rounded-[11px] border text-[10px] font-bold"
                  style={{
                    background: "var(--terracotta-tint)",
                    borderColor: "var(--border-warm)",
                    color: "var(--terracotta)",
                  }}
                >
                  PDF
                </span>
                <div className="min-w-0 flex-1">
                  <div
                    className="text-[13.5px] font-semibold"
                    style={{ color: "var(--ink)" }}
                  >
                    Résumé on file
                  </div>
                  <div className="mt-0.5 text-[11.5px]" style={{ color: "#a3927f" }}>
                    what we built your profile from
                  </div>
                </div>
              </div>
              <Link
                href="/onboarding"
                className="wm-ghost mt-[14px] flex h-[38px] w-full items-center justify-center rounded-[12px] border text-[13px] font-semibold"
                style={{ borderColor: "#e8dacb", color: "var(--ink)" }}
              >
                Upload a newer one
              </Link>
              <p className="mt-3 text-[11.5px] leading-[1.6]" style={{ color: "#a3927f" }}>
                Upload a newer one and we&apos;ll read it again. Your old resumes
                are kept.
              </p>
            </Card>

            <Card>
              <MonoLabel>What counts as a match</MonoLabel>
              <div className="mt-[14px] flex items-center gap-3">
                <span
                  className="flex-1 text-[13px] font-semibold"
                  style={{ color: "var(--ink-2)" }}
                >
                  Only show jobs scoring at least
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={minScore}
                  onChange={(e) => saveMinScore(Number(e.target.value))}
                  className="w-[110px] accent-[var(--terracotta)]"
                />
                <span
                  className="w-[22px] text-right text-[13px] font-bold tabular-nums"
                  style={{ color: "var(--ink)" }}
                >
                  {minScore}
                </span>
              </div>
              <Divider my={14} />
              <MonoLabel>Job titles you want</MonoLabel>
              <div className="mt-2.5">
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

            <DeleteAccountCard email={email} />
          </div>

          {/* Right column */}
          <div className="flex flex-col gap-4">
            <Card wide>
              <SummaryBlock
                value={draft.objective_template}
                onSave={(v) => patch({ objective_template: v })}
              />
            </Card>

            <Card wide>
              <div className="flex items-center justify-between">
                <MonoLabel>Skills · {skills.length}</MonoLabel>
                <span className="text-[11.5px]" style={{ color: "#a3927f" }}>
                  tap the × to remove one
                </span>
              </div>
              <div className="mt-3">
                <ChipEditor
                  items={skills}
                  onRemove={removeSkill}
                  onAdd={addSkill}
                  addLabel="+ Add a skill"
                />
              </div>
            </Card>

            <Card wide>
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

        <p className="mt-5 text-center text-[12px]" style={{ color: "#a3927f" }}>
          Saves as you go · your changes are used the next time we look for jobs ·{" "}
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
  { hours: 6, label: "every 6 hours" },
  { hours: 12, label: "every 12 hours" },
  { hours: 24, label: "once a day" },
  { hours: 72, label: "every 3 days" },
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
 * Auto-discovery ("What we do while you're away"): the agents' unattended
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
        <MonoLabel>What we do while you&apos;re away</MonoLabel>
        <p className="mt-3 text-[13px]" style={{ color: "#a3927f" }}>
          Loading…
        </p>
      </Card>
    );
  }

  const s = data.settings;
  const patch = (next: Partial<DiscoverySettings>) => save.mutate({ ...s, ...next });
  const sweep = data.state.last_sweep;
  const last = data.state.last_discovery;
  // Only shown once a run has actually reported a budget — pre-cap runs and
  // operator runs have none, and an invented "0 left" would read as broken.
  const budget =
    typeof last?.budget_remaining_day === "number" &&
    typeof last?.budget_granted === "number"
      ? last
      : null;

  return (
    <Card>
      <MonoLabel>What we do while you&apos;re away</MonoLabel>

      <div className="mt-[14px] flex items-center gap-3">
        <span
          className="flex-1 text-[13px] font-semibold"
          style={{ color: "var(--ink-2)" }}
        >
          Keep looking for new jobs
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
      <div className="mt-2.5 text-[11.5px]" style={{ color: "#a3927f" }}>
        {s.auto_discovery
          ? `last ${relPast(data.state.last_discovery_at)} · next ${relNext(data.next_discovery_at)}`
          : "off — run from the CLI or the button below"}
      </div>
      {budget && (
        <div
          className="mt-1 text-[11.5px]"
          style={{ color: budget.budget_capped ? "var(--honey)" : "#a3927f" }}
        >
          {budget.budget_capped
            ? `scored ${budget.budget_granted} — cycle limit reached · ${budget.budget_remaining_day} left today`
            : `${budget.budget_remaining_day} of today's scoring budget left`}
        </div>
      )}

      <Divider my={14} />

      <div className="flex items-center gap-3">
        <span
          className="flex-1 text-[13px] font-semibold"
          style={{ color: "var(--ink-2)" }}
        >
          Hide jobs that have closed
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
      <div className="mt-2.5 text-[11.5px]" style={{ color: "#a3927f" }}>
        {s.liveness_sweep
          ? `last ${relPast(data.state.last_sweep_at)} · next ${relNext(data.next_sweep_at)}`
          : "off — taken-down postings stay until acted on"}
        {sweep && ` · ${sweep.removed} removed of ${sweep.checked} checked`}
      </div>

      <div className="mt-[14px] flex gap-2">
        <button
          onClick={() => trigger.mutate("run")}
          disabled={trigger.isPending}
          className="wm-ghost h-[34px] flex-1 rounded-[11px] border text-[12px] font-semibold"
          style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
        >
          Run discovery now
        </button>
        <button
          onClick={() => trigger.mutate("sweep")}
          disabled={trigger.isPending}
          className="wm-ghost h-[34px] flex-1 rounded-[11px] border text-[12px] font-semibold"
          style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
        >
          Sweep now
        </button>
      </div>
      {trigger.isSuccess && (
        <p className="mt-2 text-[11.5px]" style={{ color: "var(--sage)" }}>
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
      className="relative h-[22px] w-[38px] flex-none rounded-full transition-colors"
      style={{ background: on ? "var(--terracotta)" : "#d9c4a8" }}
    >
      <span
        className="absolute top-[2px] h-[18px] w-[18px] rounded-full transition-all"
        style={{ left: on ? 18 : 2, background: "#fffcf8" }}
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
    <div className="mt-2.5 flex flex-wrap gap-[7px]">
      {INTERVALS.map(({ hours, label }) => {
        const active = hours === value;
        return (
          <button
            key={hours}
            onClick={() => onChange(hours)}
            className="rounded-[9px] border px-2.5 py-[5px] text-[11.5px] font-semibold"
            style={
              active
                ? {
                    background: "var(--terracotta)",
                    borderColor: "var(--terracotta)",
                    color: "#fff9f2",
                  }
                : {
                    background: "#f6ede1",
                    borderColor: "var(--border-warm-hair)",
                    color: "#a3927f",
                  }
            }
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * Delete account — the only destructive control in the app, so it is built to
 * be impossible to hit by accident: closed by default, and the confirm button
 * stays inert until the user has typed their own address. The server checks the
 * same string against the address it knows (POST /account/delete), so this
 * match is a courtesy to the user, not the guard.
 *
 * On success the Firebase Auth account is already gone server-side. The local
 * session is dropped rather than left holding an ID token that stays verifiable
 * for another hour, and the redirect is a `replace` so Back cannot return to a
 * profile page whose data no longer exists.
 */
function DeleteAccountCard({ email }: { email: string }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");

  const del = useMutation({
    mutationFn: () =>
      apiFetch<{ ok: boolean }>("/account/delete", {
        method: "POST",
        body: JSON.stringify({ confirm: typed }),
      }),
    onSuccess: async () => {
      await signOut(auth);
      router.replace("/login");
    },
  });

  const matches =
    typed.trim().toLowerCase() === email.trim().toLowerCase() && email !== "";

  return (
    <Card>
      <MonoLabel color="var(--brick)">Delete account</MonoLabel>
      {!open ? (
        <>
          <p className="mt-3 text-[11.5px] leading-[1.6]" style={{ color: "#a3927f" }}>
            removes your profile, every job and application, your tailored
            résumés and your login — permanently
          </p>
          <button
            onClick={() => setOpen(true)}
            className="wm-danger mt-3 h-[34px] w-full rounded-[12px] text-[12.5px] font-semibold"
          >
            Delete account
          </button>
        </>
      ) : (
        <>
          <p
            className="mt-3 text-[13px] leading-[1.6]"
            style={{ color: "var(--ink-2)" }}
          >
            This cannot be undone. Type <b>{email}</b> to confirm.
          </p>
          <input
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={email}
            autoComplete="off"
            className="wm-input mt-2.5 h-[38px] w-full rounded-[12px] px-3 text-[13px] outline-none"
          />
          <div className="mt-2.5 flex gap-2">
            <button
              onClick={() => {
                setOpen(false);
                setTyped("");
              }}
              disabled={del.isPending}
              className="wm-ghost h-[34px] flex-1 rounded-[12px] border text-[12.5px] font-semibold"
              style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
            >
              Cancel
            </button>
            <button
              onClick={() => del.mutate()}
              disabled={!matches || del.isPending}
              className="h-[34px] flex-1 rounded-[12px] text-[12.5px] font-semibold disabled:opacity-40"
              style={{ background: "var(--brick)", color: "#fff9f2" }}
            >
              {del.isPending ? "Deleting…" : "Delete forever"}
            </button>
          </div>
          {del.isError && (
            <p
              className="mt-2.5 text-[11.5px] leading-[1.6]"
              style={{ color: "var(--brick)" }}
            >
              {String(del.error)}
            </p>
          )}
        </>
      )}
    </Card>
  );
}

/** Warm card; `wide` is the right column's 20px padding (design 14). */
function Card({ children, wide }: { children: React.ReactNode; wide?: boolean }) {
  return (
    <div
      className={wide ? "rounded-[18px] border p-5" : "rounded-[18px] border p-[18px]"}
      style={{ background: "#fdf7ee", borderColor: "var(--border-warm-hair)" }}
    >
      {children}
    </div>
  );
}

/** "About you" — the generated candidate summary, edited in place. */
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
        <MonoLabel>About you — this is what we match jobs against</MonoLabel>
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
          className="mt-2.5 w-full rounded-[12px] p-[11px] text-[13.5px] leading-[1.7] outline-none"
          style={{
            border: "2px solid var(--terracotta)",
            background: "var(--surface-warm)",
            color: "var(--ink)",
            boxShadow: "0 0 0 3px rgba(184,83,47,0.13)",
          }}
        />
        <div className="mt-1.5 text-[11.5px]" style={{ color: "#a3927f" }}>
          <span style={{ color: "var(--terracotta-d)" }}>↵ save</span> · esc cancel
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-3">
      <div className="flex-1">
        <MonoLabel>About you — this is what we match jobs against</MonoLabel>
        <p
          className="mt-2.5 text-[13.5px] leading-[1.7]"
          style={{ color: "var(--ink-2)" }}
        >
          {value || (
            <span style={{ color: "#a3927f" }}>
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
          {i > 0 && <Divider my={14} />}
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
          className="mt-3 pl-[42px] text-[12px] font-semibold"
          style={{ color: "#a3927f" }}
        >
          Show {hidden} more…
        </button>
      )}
      {showAll && experience.length > 2 && (
        <button
          onClick={() => setShowAll(false)}
          className="mt-3 pl-[42px] text-[12px] font-semibold"
          style={{ color: "#a3927f" }}
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
  const years = `${fmtYear(role.start)}–${role.end ? fmtYear(role.end) : "Present"}`;

  return (
    <div className="mt-[14px] flex items-start gap-3 first:mt-0">
      <CompanyTile initial={initial(role.company)} hue={tileHue(role.company)} size="sm" />
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
                className="w-full rounded-[10px] border p-2 text-[12.5px] leading-[1.6] outline-none"
                style={{
                  background: "var(--surface-warm)",
                  borderColor: "#e8dacb",
                  color: "var(--ink-2)",
                }}
              />
            ))}
          </div>
        ) : (
          <>
            <div className="text-[13.5px]" style={{ color: "var(--ink-4)" }}>
              <b style={{ color: "var(--ink)" }}>{role.role}</b> · {role.company}{" "}
              · {years}
            </div>
            {expanded && role.bullets.length > 0 && (
              <ul className="mt-2 flex list-disc flex-col gap-[5px] pl-[18px]">
                {role.bullets.map((b, i) => (
                  <li
                    key={i}
                    className="text-[12.5px] leading-[1.6]"
                    style={{ color: "var(--ink-4)" }}
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
      className="h-8 min-w-0 flex-1 rounded-[10px] px-2 text-[13px] outline-none"
      style={{
        border: "2px solid var(--terracotta)",
        background: "var(--surface-warm)",
        color: "var(--ink)",
        boxShadow: "0 0 0 3px rgba(184,83,47,0.13)",
      }}
    />
  );
}

function fmtYear(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : String(d.getFullYear());
}
