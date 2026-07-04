// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { markFirstRun } from "@/lib/session";
import type { Profile, ProfileResponse } from "@/lib/types";
import { avatarColor, initial, resolveUserAvatar } from "@/lib/ui";
import {
  ChipEditor,
  Divider,
  InlineText,
  MonoLabel,
  PencilBtn,
} from "@/components/editable";

/**
 * Confirm & correct (mock 05): every parsed field is click-to-edit before
 * Matching runs, so a bad parse never becomes bad matches. Edits patch the
 * profile doc on "Looks good — find me jobs".
 */
export default function OnboardingReviewPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [draft, setDraft] = useState<Profile | null>(null);
  const [fieldsCorrected, setFieldsCorrected] = useState(0);
  const [skillsRemoved, setSkillsRemoved] = useState(0);

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

  const { data, isLoading, error } = useQuery({
    queryKey: ["profile-review"],
    queryFn: () => apiFetch<ProfileResponse>("/profile"),
    enabled: !!user,
  });

  // Seed the editable draft once the extracted profile arrives (render-time
  // setState, guarded so it runs once — the sanctioned pattern over an effect).
  if (data?.profile && draft === null) setDraft(data.profile);

  // If the user landed here without a profile (e.g. refresh after onboarding),
  // bounce them back to the upload step.
  useEffect(() => {
    if (data && data.profile === null) router.push("/onboarding");
  }, [data, router]);

  const [saved, setSaved] = useState(false);
  const save = useMutation({
    mutationFn: (profile: Profile) =>
      apiFetch("/profile", { method: "PUT", body: JSON.stringify(profile) }),
    onSuccess: () => {
      markFirstRun();
      setSaved(true);
    },
  });

  // After the celebratory "Profile saved" beat, drop the user into the queue.
  useEffect(() => {
    if (!saved) return;
    const t = setTimeout(() => router.push("/"), 1700);
    return () => clearTimeout(t);
  }, [saved, router]);

  if (saved) return <SavedView />;

  if (loading || !user || isLoading || !draft) {
    return <div className="p-8" style={{ color: "var(--muted)" }}>Loading…</div>;
  }
  if (error) {
    return (
      <div className="p-8" style={{ color: "var(--danger)" }}>
        Failed to load your profile: {String(error)}
      </div>
    );
  }

  const av = resolveUserAvatar(draft.full_name, draft.email || user.email);
  const skills = Object.values(draft.skills ?? {}).flat();

  function patch(next: Partial<Profile>, corrected = true) {
    setDraft((d) => (d ? { ...d, ...next } : d));
    if (corrected) setFieldsCorrected((n) => n + 1);
  }

  function removeSkill(label: string) {
    setDraft((d) => {
      if (!d) return d;
      const next: Record<string, string[]> = {};
      for (const [cat, items] of Object.entries(d.skills ?? {})) {
        const kept = items.filter((s) => s !== label);
        if (kept.length) next[cat] = kept;
      }
      return { ...d, skills: next };
    });
    setSkillsRemoved((n) => n + 1);
  }

  function addSkill(label: string) {
    setDraft((d) => {
      if (!d) return d;
      const next = { ...(d.skills ?? {}) };
      const cat = Object.keys(next)[0] ?? "skills";
      next[cat] = [...(next[cat] ?? []), label];
      return { ...d, skills: next };
    });
    setSkillsRemoved((n) => Math.max(0, n - 1));
  }

  return (
    <main className="mx-auto w-full max-w-[560px] flex-1 px-6 py-11">
      <h1
        className="text-center text-[23px] font-semibold tracking-tight"
        style={{ color: "var(--text)" }}
      >
        {"Here's what Hermes learned"}
      </h1>
      <p
        className="mt-2 text-center text-[13.5px] leading-normal"
        style={{ color: "var(--muted)" }}
      >
        Everything is editable — click any field to correct it before we start
        matching.
      </p>

      <div
        className="mt-[22px] rounded-xl border p-[18px]"
        style={{ background: "var(--surface)", borderColor: "var(--border)" }}
      >
        {/* Name row — sets the avatar initials */}
        <div className="flex items-center gap-[13px]">
          <span
            className="flex h-11 w-11 flex-none items-center justify-center rounded-full text-base font-bold"
            style={{ background: "var(--text)", color: "var(--surface)" }}
          >
            {av.kind === "glyph" ? "•" : av.text}
          </span>
          <div className="min-w-0 flex-1">
            <MonoLabel>Name</MonoLabel>
            <div className="mt-1">
              <InlineText
                value={draft.full_name}
                textClass="text-base font-semibold"
                placeholder="Your name"
                onSave={(v) => v !== draft.full_name && patch({ full_name: v })}
              />
            </div>
          </div>
          <span
            className="flex-none font-mono text-[11px] font-medium"
            style={{ color: "var(--good)" }}
          >
            sets your avatar
          </span>
        </div>

        <Divider />

        {/* Target role + location */}
        <div className="flex gap-4">
          <div className="flex-1">
            <MonoLabel>Target role</MonoLabel>
            <div className="mt-1.5">
              <InlineText
                value={draft.preferences.target_titles?.[0] ?? ""}
                placeholder="e.g. Senior Backend Engineer"
                onSave={(v) => {
                  const titles = [...(draft.preferences.target_titles ?? [])];
                  if (titles.length) titles[0] = v;
                  else titles.push(v);
                  patch({
                    preferences: { ...draft.preferences, target_titles: titles },
                  });
                }}
              />
            </div>
          </div>
          <div className="w-[170px]">
            <MonoLabel>Location</MonoLabel>
            <div className="mt-1.5">
              <InlineText
                value={draft.location}
                placeholder="City, State"
                onSave={(v) => v !== draft.location && patch({ location: v })}
              />
            </div>
          </div>
        </div>

        <Divider />

        {/* Skills */}
        <div className="flex items-center justify-between">
          <MonoLabel>Skills · {skills.length} found</MonoLabel>
          <span
            className="font-mono text-[11px] font-medium"
            style={{ color: "var(--subtle)" }}
          >
            × to remove
          </span>
        </div>
        <div className="mt-2.5">
          <ChipEditor items={skills} onRemove={removeSkill} onAdd={addSkill} />
        </div>

        <Divider />

        {/* Experience */}
        <MonoLabel>Experience · {draft.experience.length} roles</MonoLabel>
        <ExperienceRows
          experience={draft.experience}
          onChange={(experience) => patch({ experience })}
        />
      </div>

      <div className="mt-[18px] flex gap-2.5">
        <button
          onClick={() => save.mutate(draft)}
          disabled={save.isPending}
          className="h-11 flex-1 rounded-[9px] text-sm font-semibold disabled:opacity-50"
          style={{ background: "var(--text)", color: "var(--surface)" }}
        >
          {save.isPending ? "Saving…" : "Looks good — find me jobs →"}
        </button>
        <Link
          href="/onboarding"
          className="flex h-11 items-center rounded-[9px] border px-[18px] text-[13px] font-semibold"
          style={{
            background: "var(--surface)",
            borderColor: "var(--border)",
            color: "var(--label)",
          }}
        >
          Re-upload
        </Link>
      </div>

      {save.isError && (
        <p className="mt-3 text-center text-sm" style={{ color: "var(--danger)" }}>
          Could not save: {String(save.error)}
        </p>
      )}

      <p
        className="mt-3 text-center font-mono text-[11px] font-medium"
        style={{ color: "var(--subtle)" }}
      >
        edits saved to profiles/{"{uid}"}
        {fieldsCorrected > 0 &&
          ` · ${fieldsCorrected} field${fieldsCorrected === 1 ? "" : "s"} corrected`}
        {skillsRemoved > 0 &&
          ` · ${skillsRemoved} skill${skillsRemoved === 1 ? "" : "s"} removed`}
      </p>
    </main>
  );
}

function ExperienceRows({
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
    <div className="mt-2.5 flex flex-col gap-[9px]">
      {visible.map((role, i) => (
        <ExperienceRow
          key={i}
          role={role}
          onChange={(r) =>
            onChange(experience.map((e, j) => (j === i ? r : e)))
          }
        />
      ))}
      {hidden > 0 && (
        <button
          onClick={() => setShowAll(true)}
          className="ml-[38px] self-start text-xs font-medium"
          style={{ color: "var(--muted)" }}
        >
          Show {hidden} more…
        </button>
      )}
    </div>
  );
}

function ExperienceRow({
  role,
  onChange,
}: {
  role: Profile["experience"][number];
  onChange: (r: Profile["experience"][number]) => void;
}) {
  const [editing, setEditing] = useState(false);
  const av = avatarColor(role.company);
  const years = `${fmtYear(role.start)}–${role.end ? fmtYear(role.end) : "Present"}`;

  return (
    <div className="flex items-center gap-2.5">
      <span
        className="flex h-7 w-7 flex-none items-center justify-center rounded-[7px] text-xs font-bold"
        style={{ background: av.bg, color: av.color }}
      >
        {initial(role.company)}
      </span>
      {editing ? (
        <span className="flex flex-1 gap-2">
          <RowInput
            value={role.role}
            placeholder="Role"
            onCommit={(v) => onChange({ ...role, role: v })}
          />
          <RowInput
            value={role.company}
            placeholder="Company"
            onCommit={(v) => onChange({ ...role, company: v })}
          />
          <button
            onClick={() => setEditing(false)}
            className="text-xs font-semibold"
            style={{ color: "var(--accent)" }}
          >
            done
          </button>
        </span>
      ) : (
        <>
          <span className="flex-1 text-[13.5px]" style={{ color: "var(--label)" }}>
            <b style={{ color: "var(--text)" }}>{role.role}</b> · {role.company} ·{" "}
            {years}
          </span>
          <PencilBtn onClick={() => setEditing(true)} />
        </>
      )}
    </div>
  );
}

function RowInput({
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
      onBlur={() => draft.trim() && onCommit(draft.trim())}
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

function SavedView() {
  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <div className="h-phase w-[420px] max-w-full text-center">
        <div
          className="h-pop mx-auto flex h-14 w-14 items-center justify-center rounded-full border text-2xl"
          style={{
            background: "var(--good-bg)",
            borderColor: "var(--good-border)",
            color: "var(--good)",
          }}
        >
          ✓
        </div>
        <h1
          className="mt-[18px] text-[23px] font-semibold tracking-tight"
          style={{ color: "var(--text)" }}
        >
          Profile saved
        </h1>
        <p className="mt-2 text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
          Saved to your account. Discovery and Matching are running now.
        </p>
        <div
          className="mt-5 inline-flex items-center gap-2.5 text-[13px] font-medium"
          style={{ color: "var(--label)" }}
        >
          <span
            className="inline-block h-4 w-4 rounded-full"
            style={{
              border: "2px solid var(--accent)",
              borderTopColor: "transparent",
              animation: "hspin 0.8s linear infinite",
            }}
          />
          Finding your first jobs…
        </div>
      </div>
    </main>
  );
}
