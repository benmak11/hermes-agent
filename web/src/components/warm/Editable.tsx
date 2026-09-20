// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useState } from "react";

// Warm-skinned fork of components/editable.tsx (facelift PR 5). Same exports,
// props and logic; only the palette differs. The grey original stays for
// /app/profile and /app/interviews until PR 6–8 switch them over.

/** 10.5px uppercase micro-label (card/field headers). */
export function MonoLabel({
  children,
  color = "#a3927f",
}: {
  children: React.ReactNode;
  color?: string;
}) {
  return (
    <div
      className="text-[10.5px] font-bold uppercase"
      style={{ color, letterSpacing: "0.12em" }}
    >
      {children}
    </div>
  );
}

export function Divider({ my = 16 }: { my?: number }) {
  return (
    <div
      className="h-px"
      style={{ background: "#f0e3d3", margin: `${my}px 0` }}
    />
  );
}

export function PencilBtn({
  onClick,
  label = "Edit",
}: {
  onClick: () => void;
  label?: string;
}) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      className="flex-none text-xs"
      style={{ color: "#a3927f" }}
    >
      ✎
    </button>
  );
}

/**
 * Click-to-edit text: the ✎ swaps the value for an inline input with a 2px
 * terracotta border + soft ring, an `↵ save · esc cancel` hint, and the
 * previous value shown as history while editing.
 */
export function InlineText({
  value,
  onSave,
  textClass = "text-sm font-semibold",
  placeholder,
}: {
  value: string;
  onSave: (v: string) => void;
  textClass?: string;
  placeholder?: string;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);

  if (!editing) {
    return (
      <span className="flex items-center gap-2">
        <span className={textClass} style={{ color: "var(--ink)" }}>
          {value || (
            <span style={{ color: "#a3927f" }}>{placeholder ?? "—"}</span>
          )}
        </span>
        <PencilBtn
          onClick={() => {
            setDraft(value);
            setEditing(true);
          }}
        />
      </span>
    );
  }

  return (
    <span className="block">
      <span
        className="flex h-10 items-center rounded-xl px-[11px]"
        style={{
          border: "2px solid var(--terracotta)",
          background: "var(--surface-warm)",
          boxShadow: "0 0 0 3px rgba(184,83,47,0.13)",
        }}
      >
        <input
          autoFocus
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              onSave(draft.trim());
              setEditing(false);
            }
            if (e.key === "Escape") setEditing(false);
          }}
          className={`w-full bg-transparent outline-none ${textClass}`}
          style={{ color: "var(--ink)" }}
        />
      </span>
      <span
        className="mt-1.5 block text-[11.5px] font-medium"
        style={{ color: "#a3927f" }}
      >
        was: “{value || "—"}” ·{" "}
        <span style={{ color: "var(--terracotta-d)" }}>↵ save</span> · esc cancel
      </span>
    </span>
  );
}

/**
 * Removable chip list: × to remove, a brief strikethrough + undo state for a
 * just-removed chip, and a dashed `+ add a skill` chip that opens an inline
 * input.
 */
export function ChipEditor({
  items,
  onRemove,
  onAdd,
  addLabel = "+ add a skill",
}: {
  items: string[];
  onRemove: (label: string) => void;
  onAdd: (label: string) => void;
  addLabel?: string;
}) {
  const [justRemoved, setJustRemoved] = useState<string[]>([]);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");

  function remove(label: string) {
    onRemove(label);
    setJustRemoved((r) => [...r, label]);
    // The undo affordance is brief — the ghost chip clears itself.
    setTimeout(
      () => setJustRemoved((r) => r.filter((l) => l !== label)),
      6000,
    );
  }

  function undo(label: string) {
    setJustRemoved((r) => r.filter((l) => l !== label));
    onAdd(label);
  }

  function commitAdd() {
    const v = draft.trim();
    if (v) onAdd(v);
    setDraft("");
    setAdding(false);
  }

  return (
    <div className="flex flex-wrap gap-2">
      {items.map((label) => (
        <span
          key={label}
          className="inline-flex items-center gap-1.5 rounded-full border px-[13px] py-1.5 text-[13.5px]"
          style={{
            background: "var(--surface-warm)",
            borderColor: "#e8dacb",
            color: "var(--ink-2)",
          }}
        >
          {label}
          <button
            onClick={() => remove(label)}
            aria-label={`Remove ${label}`}
            className="text-[11px]"
            style={{ color: "#a3927f" }}
          >
            ×
          </button>
        </span>
      ))}
      {justRemoved.map((label) => (
        <span
          key={`removed-${label}`}
          className="inline-flex items-center gap-1.5 rounded-full px-[13px] py-1.5 text-[13.5px] line-through"
          style={{
            background: "var(--surface-warm)",
            border: "1px dashed #f2cfc3",
            color: "#a3927f",
          }}
        >
          {label}
          <button
            onClick={() => undo(label)}
            className="text-[11px] font-bold no-underline"
            style={{ color: "var(--brick)", textDecoration: "none" }}
          >
            undo
          </button>
        </span>
      ))}
      {adding ? (
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitAdd();
            if (e.key === "Escape") {
              setDraft("");
              setAdding(false);
            }
          }}
          onBlur={commitAdd}
          placeholder="skill…"
          className="h-[34px] w-32 rounded-full px-3 text-[13.5px] outline-none"
          style={{
            background: "var(--surface-warm)",
            border: "2px solid var(--terracotta)",
            color: "var(--ink)",
            boxShadow: "0 0 0 3px rgba(184,83,47,0.13)",
          }}
        />
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="inline-flex items-center gap-1 rounded-full px-[13px] py-1.5 text-[13.5px]"
          style={{
            background: "#fdf7ee",
            border: "1px dashed #d9c4a8",
            color: "#96826f",
          }}
        >
          {addLabel}
        </button>
      )}
    </div>
  );
}
