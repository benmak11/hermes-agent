// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { APP_HOME } from "@/lib/nav";
import { markFirstRun } from "@/lib/session";
import type { Profile, ProfileResponse } from "@/lib/types";
import { initial, resolveUserAvatar } from "@/lib/ui";
import { CompanyTile, tileHue } from "@/components/warm/CompanyTile";
import {
  ChipEditor,
  Divider,
  InlineText,
  MonoLabel,
  PencilBtn,
} from "@/components/warm/Editable";
import { Pill } from "@/components/warm/Pill";
import { CARD, SERIF } from "@/components/warm/styles";

/** The tinted sub-card each field group sits in (design 05). */
const SUB_CARD: React.CSSProperties = {
  borderRadius: 18,
  border: "1px solid var(--border-warm-hair)",
  background: "#fdf7ee",
  padding: "16px 18px",
};

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Confirm & correct (mock 05): every parsed field is click-to-edit before
 * Matching runs, so a bad parse never becomes bad matches. Edits patch the
 * profile doc on "Looks right — find me jobs".
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
    const t = setTimeout(() => router.push(APP_HOME), 1700);
    return () => clearTimeout(t);
  }, [saved, router]);

  if (saved) return <SavedView />;

  if (loading || !user || isLoading || !draft) {
    return <div className="p-8" style={{ color: "var(--ink-4)" }}>Loading…</div>;
  }
  if (error) {
    return (
      <div className="p-8" style={{ color: "var(--brick)" }}>
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
    <main className="mx-auto w-full flex-1 px-6 py-8" style={{ maxWidth: 872 }}>
      <div style={{ ...CARD, padding: "32px 36px" }}>
        <h1
          className="text-[32px] font-normal"
          style={{ fontFamily: SERIF, lineHeight: 1.15, color: "var(--ink)" }}
        >
          {"Here's what we learned"}
        </h1>
        <p
          className="mt-2.5 text-sm"
          style={{ color: "var(--ink-4)", lineHeight: 1.55 }}
        >
          Change anything — you know yourself better than we do.
        </p>

        {/* Name row — sets the avatar initials */}
        <div className="mt-[22px] flex items-center gap-[13px]">
          <span
            className="flex h-11 w-11 flex-none items-center justify-center rounded-full text-base font-bold"
            style={{ background: "var(--ink)", color: "#fff9f2" }}
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
            className="flex-none text-[11.5px] font-semibold"
            style={{ color: "var(--sage)" }}
          >
            sets your avatar
          </span>
        </div>

        <Divider />

        {/* Target role + location & work style */}
        <div className="flex flex-wrap gap-3.5">
          <div style={{ ...SUB_CARD, flex: "1 1 240px" }}>
            <MonoLabel>Target role</MonoLabel>
            <div className="mt-2">
              <InlineText
                value={draft.preferences.target_titles?.[0] ?? ""}
                textClass="text-base font-semibold"
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
          <div style={{ ...SUB_CARD, flex: "1 1 240px" }}>
            <MonoLabel>Location &amp; work style</MonoLabel>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <InlineText
                value={draft.location}
                textClass="text-base font-semibold"
                placeholder="City, State"
                onSave={(v) => v !== draft.location && patch({ location: v })}
              />
              {/* Read-only here; edited on /app/profile. */}
              {draft.preferences.remote_policy?.map((s) => (
                <Pill key={s} tone="warn">
                  {cap(s)}
                </Pill>
              ))}
            </div>
          </div>
        </div>

        {/* Skills */}
        <div className="mt-3.5" style={SUB_CARD}>
          <div className="flex items-center justify-between">
            <MonoLabel>Skills · {skills.length} found</MonoLabel>
            <span className="text-[11.5px] font-medium" style={{ color: "#a3927f" }}>
              × to remove
            </span>
          </div>
          <div className="mt-3">
            <ChipEditor items={skills} onRemove={removeSkill} onAdd={addSkill} />
          </div>
        </div>

        {/* Experience */}
        <div className="mt-3.5" style={SUB_CARD}>
          <MonoLabel>Experience · {draft.experience.length} roles</MonoLabel>
          <ExperienceRows
            experience={draft.experience}
            onChange={(experience) => patch({ experience })}
          />
        </div>

        <div
          className="mt-6 flex flex-wrap items-center justify-between gap-5 pt-5"
          style={{ borderTop: "1px solid #f0e3d3" }}
        >
          <span className="text-[13.5px]" style={{ color: "var(--ink-4)" }}>
            You can refine this anytime from{" "}
            <b style={{ color: "var(--ink)" }}>Profile</b>.
          </span>
          <div className="flex gap-2.5">
            <Link
              href="/onboarding"
              className="wm-ghost flex h-[46px] items-center rounded-[13px] border px-[18px] text-[13.5px] font-semibold"
              style={{ borderColor: "#e8dacb", color: "var(--ink-2)" }}
            >
              Re-upload
            </Link>
            <button
              onClick={() => save.mutate(draft)}
              disabled={save.isPending}
              className="wm-cta h-[46px] rounded-[13px] px-6 text-[14.5px] font-semibold"
            >
              {save.isPending ? "Saving…" : "Looks right — find me jobs →"}
            </button>
          </div>
        </div>

        {save.isError && (
          <p className="mt-3 text-center text-[13.5px]" style={{ color: "var(--brick)" }}>
            Could not save: {String(save.error)}
          </p>
        )}

        <p
          className="mt-3 text-center text-[11.5px] font-medium"
          style={{ color: "#a3927f" }}
        >
          edits saved to profiles/{"{uid}"}
          {fieldsCorrected > 0 &&
            ` · ${fieldsCorrected} field${fieldsCorrected === 1 ? "" : "s"} corrected`}
          {skillsRemoved > 0 &&
            ` · ${skillsRemoved} skill${skillsRemoved === 1 ? "" : "s"} removed`}
        </p>
      </div>
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
          style={{ color: "var(--ink-4)" }}
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
  const years = `${fmtYear(role.start)}–${role.end ? fmtYear(role.end) : "Present"}`;

  return (
    <div className="flex items-center gap-2.5">
      <CompanyTile initial={initial(role.company)} hue={tileHue(role.company)} size="sm" />
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
            className="wm-link text-xs font-semibold"
          >
            done
          </button>
        </span>
      ) : (
        <>
          <span className="flex-1 text-[13.5px]" style={{ color: "var(--ink-2)" }}>
            <b style={{ color: "var(--ink)" }}>{role.role}</b> · {role.company} ·{" "}
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
      className="h-8 min-w-0 flex-1 rounded-[10px] px-2 text-[13px] outline-none"
      style={{
        background: "var(--surface-warm)",
        border: "2px solid var(--terracotta)",
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

function SavedView() {
  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <div className="h-phase w-[420px] max-w-full text-center">
        <div
          className="h-pop mx-auto flex h-14 w-14 items-center justify-center rounded-full border text-2xl"
          style={{
            background: "var(--sage-tint)",
            borderColor: "#cfe0c8",
            color: "var(--sage)",
          }}
        >
          ✓
        </div>
        <h1
          className="mt-[18px] text-[30px] font-normal"
          style={{ fontFamily: SERIF, lineHeight: 1.15, color: "var(--ink)" }}
        >
          Profile saved
        </h1>
        <p className="mt-2 text-[14.5px] leading-relaxed" style={{ color: "var(--ink-4)" }}>
          Saved to your account. Discovery and Matching are running now.
        </p>
        <div
          className="mt-5 inline-flex items-center gap-2.5 text-[13.5px] font-medium"
          style={{ color: "var(--ink-3)" }}
        >
          <span
            className="inline-block h-4 w-4 rounded-full"
            style={{
              border: "2px solid var(--terracotta)",
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
