// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useState } from "react";

import type { Profile, ProfileExperience, RemoteStyle } from "@/lib/types";
import { avatarColor, initial } from "@/lib/ui";

const REMOTE_OPTIONS: RemoteStyle[] = ["remote", "hybrid", "onsite"];

/**
 * Controlled editor for the resume-derived profile. Shared by the onboarding
 * review screen and the standalone Profile page — the wrapper supplies the
 * heading, step rail, and the commit (save) action.
 */
export function ProfileEditor({
  value,
  onChange,
}: {
  value: Profile;
  onChange: (next: Profile) => void;
}) {
  const skillEntries = Object.entries(value.skills ?? {});
  const totalSkills = skillEntries.reduce((n, [, v]) => n + v.length, 0);

  function setTargetTitle(title: string) {
    const titles = [...(value.preferences.target_titles ?? [])];
    if (titles.length) titles[0] = title;
    else titles.push(title);
    onChange({ ...value, preferences: { ...value.preferences, target_titles: titles } });
  }

  function toggleRemote(style: RemoteStyle) {
    const cur = value.preferences.remote_policy ?? [];
    const next = cur.includes(style)
      ? cur.filter((s) => s !== style)
      : [...cur, style];
    onChange({ ...value, preferences: { ...value.preferences, remote_policy: next } });
  }

  function removeSkill(category: string, idx: number) {
    const next = { ...value.skills };
    next[category] = next[category].filter((_, i) => i !== idx);
    if (next[category].length === 0) delete next[category];
    onChange({ ...value, skills: next });
  }

  function addSkill(label: string) {
    const trimmed = label.trim();
    if (!trimmed) return;
    const next = { ...value.skills };
    const category = Object.keys(next)[0] ?? "skills";
    next[category] = [...(next[category] ?? []), trimmed];
    onChange({ ...value, skills: next });
  }

  function updateExperience(idx: number, role: ProfileExperience) {
    const next = value.experience.map((e, i) => (i === idx ? role : e));
    onChange({ ...value, experience: next });
  }

  return (
    <div>
      {/* target + location row */}
      <div className="mt-[22px] flex flex-col gap-3.5 sm:flex-row">
        <Card className="flex-1">
          <FieldLabel>Target role</FieldLabel>
          <EditableText
            value={value.preferences.target_titles?.[0] ?? ""}
            placeholder="e.g. Senior Backend Engineer"
            onSave={setTargetTitle}
          />
        </Card>
        <Card className="flex-1">
          <FieldLabel>Location · work style</FieldLabel>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <EditableText
              value={value.location}
              placeholder="City, State"
              onSave={(loc) => onChange({ ...value, location: loc })}
              inline
            />
            {REMOTE_OPTIONS.map((style) => {
              const on = (value.preferences.remote_policy ?? []).includes(style);
              return (
                <button
                  key={style}
                  onClick={() => toggleRemote(style)}
                  className="rounded-full border px-2 py-0.5 font-mono text-[11px] font-semibold capitalize"
                  style={{
                    background: on ? "var(--accent-bg)" : "transparent",
                    borderColor: on ? "var(--accent-border)" : "var(--border)",
                    color: on ? "var(--accent-text)" : "var(--subtle)",
                  }}
                >
                  {style}
                </button>
              );
            })}
          </div>
        </Card>
      </div>

      {/* skills */}
      <Card className="mt-3.5">
        <div className="flex items-center justify-between">
          <FieldLabel>Skills &amp; expertise</FieldLabel>
          <SkillAdder onAdd={addSkill} />
        </div>
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {skillEntries.flatMap(([category, items]) =>
            items.map((skill, i) => (
              <span
                key={`${category}-${i}`}
                className="inline-flex items-center gap-1.5 rounded-[7px] border px-2.5 py-1 text-[13px]"
                style={{
                  background: "var(--surface-2)",
                  borderColor: "var(--border)",
                  color: "var(--label)",
                }}
              >
                {skill}
                <button
                  onClick={() => removeSkill(category, i)}
                  aria-label={`Remove ${skill}`}
                  style={{ color: "var(--subtle)" }}
                >
                  ×
                </button>
              </span>
            )),
          )}
          {totalSkills === 0 && (
            <span className="text-[13px]" style={{ color: "var(--subtle)" }}>
              No skills yet — add a few.
            </span>
          )}
        </div>
      </Card>

      {/* experience */}
      <ExperienceList
        experience={value.experience}
        onUpdate={updateExperience}
      />
    </div>
  );
}

function ExperienceList({
  experience,
  onUpdate,
}: {
  experience: ProfileExperience[];
  onUpdate: (idx: number, role: ProfileExperience) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? experience : experience.slice(0, 2);
  const hidden = experience.length - visible.length;

  return (
    <div className="mt-3.5">
      <div className="mb-2.5 flex items-center justify-between">
        <FieldLabel>Experience · master bullets</FieldLabel>
        <span className="font-mono text-xs" style={{ color: "var(--subtle)" }}>
          {experience.length} {experience.length === 1 ? "role" : "roles"}
        </span>
      </div>
      <div className="flex flex-col gap-2.5">
        {visible.map((role, i) => (
          <RoleCard key={i} role={role} onChange={(r) => onUpdate(i, r)} />
        ))}
      </div>
      {hidden > 0 && (
        <div className="mt-2.5 text-center text-[13px]" style={{ color: "var(--muted)" }}>
          + {hidden} more {hidden === 1 ? "role" : "roles"} ·{" "}
          <button
            onClick={() => setShowAll(true)}
            className="font-semibold"
            style={{ color: "var(--accent)" }}
          >
            Show all
          </button>
        </div>
      )}
      {showAll && experience.length > 2 && (
        <div className="mt-2.5 text-center">
          <button
            onClick={() => setShowAll(false)}
            className="text-[13px] font-semibold"
            style={{ color: "var(--accent)" }}
          >
            Show less
          </button>
        </div>
      )}
    </div>
  );
}

function RoleCard({
  role,
  onChange,
}: {
  role: ProfileExperience;
  onChange: (role: ProfileExperience) => void;
}) {
  const [editing, setEditing] = useState(false);
  const av = avatarColor(role.company);
  const period = `${fmtYear(role.start)} — ${role.end ? fmtYear(role.end) : "Present"}`;

  function setBullet(idx: number, text: string) {
    const bullets = role.bullets.map((b, i) => (i === idx ? { ...b, text } : b));
    onChange({ ...role, bullets });
  }
  function removeBullet(idx: number) {
    onChange({ ...role, bullets: role.bullets.filter((_, i) => i !== idx) });
  }
  function addBullet() {
    onChange({ ...role, bullets: [...role.bullets, { text: "", tags: [] }] });
  }

  return (
    <Card>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-[11px]">
          <span
            className="flex h-8 w-8 flex-none items-center justify-center rounded-lg text-[13px] font-bold"
            style={{ background: av.bg, color: av.color }}
          >
            {initial(role.company)}
          </span>
          <div className="min-w-0">
            {editing ? (
              <div className="flex flex-col gap-1">
                <Input
                  value={role.role}
                  onChange={(v) => onChange({ ...role, role: v })}
                  placeholder="Role"
                />
                <Input
                  value={role.company}
                  onChange={(v) => onChange({ ...role, company: v })}
                  placeholder="Company"
                />
              </div>
            ) : (
              <>
                <div
                  className="truncate text-[15px] font-semibold"
                  style={{ color: "var(--text)" }}
                >
                  {role.role}
                </div>
                <div className="mt-0.5 text-[13px]" style={{ color: "var(--muted)" }}>
                  {role.company} · {period}
                </div>
              </>
            )}
          </div>
        </div>
        <button
          onClick={() => setEditing((e) => !e)}
          className="flex-none text-xs font-medium"
          style={{ color: "var(--accent)" }}
        >
          {editing ? "Done" : "Edit"}
        </button>
      </div>

      {editing ? (
        <div className="mt-3 flex flex-col gap-2">
          {role.bullets.map((b, i) => (
            <div key={i} className="flex items-start gap-2">
              <textarea
                value={b.text}
                onChange={(e) => setBullet(i, e.target.value)}
                rows={2}
                className="flex-1 rounded-[8px] border p-2 text-[13px] outline-none focus:ring-[3px]"
                style={
                  {
                    background: "var(--surface)",
                    borderColor: "var(--border)",
                    color: "var(--label)",
                    "--tw-ring-color": "var(--ring)",
                  } as React.CSSProperties
                }
              />
              <button
                onClick={() => removeBullet(i)}
                aria-label="Remove bullet"
                className="mt-1 text-sm"
                style={{ color: "var(--subtle)" }}
              >
                ×
              </button>
            </div>
          ))}
          <button
            onClick={addBullet}
            className="self-start text-xs font-semibold"
            style={{ color: "var(--accent)" }}
          >
            + Add bullet
          </button>
        </div>
      ) : (
        role.bullets.length > 0 && (
          <ul className="mt-3 flex list-disc flex-col gap-1.5 pl-[18px]">
            {role.bullets.map((b, i) => (
              <li
                key={i}
                className="text-[13px] leading-relaxed"
                style={{ color: "var(--label)" }}
              >
                {b.text}
              </li>
            ))}
          </ul>
        )
      )}
    </Card>
  );
}

function SkillAdder({ onAdd }: { onAdd: (label: string) => void }) {
  const [open, setOpen] = useState(false);
  const [val, setVal] = useState("");
  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="text-xs font-medium"
        style={{ color: "var(--accent)" }}
      >
        + Add
      </button>
    );
  }
  function commit() {
    onAdd(val);
    setVal("");
    setOpen(false);
  }
  return (
    <input
      autoFocus
      value={val}
      onChange={(e) => setVal(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") setOpen(false);
      }}
      onBlur={commit}
      placeholder="Add skill…"
      className="h-7 rounded-[7px] border px-2 text-[13px] outline-none focus:ring-[3px]"
      style={
        {
          background: "var(--surface)",
          borderColor: "var(--border)",
          color: "var(--text)",
          "--tw-ring-color": "var(--ring)",
        } as React.CSSProperties
      }
    />
  );
}

function EditableText({
  value,
  placeholder,
  onSave,
  inline,
}: {
  value: string;
  placeholder?: string;
  onSave: (v: string) => void;
  inline?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  if (editing) {
    return (
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            onSave(draft);
            setEditing(false);
          }
          if (e.key === "Escape") setEditing(false);
        }}
        onBlur={() => {
          onSave(draft);
          setEditing(false);
        }}
        placeholder={placeholder}
        className={`${inline ? "" : "mt-1.5 w-full"} h-8 rounded-[8px] border px-2 text-[15px] font-semibold outline-none focus:ring-[3px]`}
        style={
          {
            background: "var(--surface)",
            borderColor: "var(--border)",
            color: "var(--text)",
            "--tw-ring-color": "var(--ring)",
          } as React.CSSProperties
        }
      />
    );
  }
  return (
    <span
      className={inline ? "flex items-center gap-2" : "mt-1.5 flex items-center justify-between"}
    >
      <span className="text-[15px] font-semibold" style={{ color: "var(--text)" }}>
        {value || <span style={{ color: "var(--subtle)" }}>{placeholder}</span>}
      </span>
      <button
        onClick={() => {
          setDraft(value);
          setEditing(true);
        }}
        className="text-xs font-medium"
        style={{ color: "var(--accent)" }}
      >
        Edit
      </button>
    </span>
  );
}

function Input({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="h-8 rounded-[8px] border px-2 text-sm outline-none focus:ring-[3px]"
      style={
        {
          background: "var(--surface)",
          borderColor: "var(--border)",
          color: "var(--text)",
          "--tw-ring-color": "var(--ring)",
        } as React.CSSProperties
      }
    />
  );
}

function Card({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-xl border p-4 ${className}`}
      style={{ background: "var(--surface)", borderColor: "var(--border)" }}
    >
      {children}
    </div>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="font-mono text-[10px] font-semibold uppercase tracking-wide"
      style={{ color: "var(--subtle)" }}
    >
      {children}
    </div>
  );
}

function fmtYear(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : String(d.getFullYear());
}
