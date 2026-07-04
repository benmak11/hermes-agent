// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useState } from "react";

/** 10px mono uppercase micro-label (mock spec for card/field headers). */
export function MonoLabel({
  children,
  color = "var(--subtle)",
}: {
  children: React.ReactNode;
  color?: string;
}) {
  return (
    <div
      className="font-mono text-[10px] font-semibold uppercase"
      style={{ color, letterSpacing: "0.05em" }}
    >
      {children}
    </div>
  );
}

export function Divider({ my = 16 }: { my?: number }) {
  return (
    <div
      className="h-px"
      style={{ background: "var(--divider)", margin: `${my}px 0` }}
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
      style={{ color: "var(--subtle)" }}
    >
      ✎
    </button>
  );
}

/**
 * Click-to-edit text (mock spec): the ✎ swaps the value for an inline input
 * with a 2px accent border + soft ring, an `↵ save · esc cancel` hint, and the
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
        <span className={textClass} style={{ color: "var(--text)" }}>
          {value || (
            <span style={{ color: "var(--subtle)" }}>{placeholder ?? "—"}</span>
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
        className="flex h-[38px] items-center rounded-lg px-[11px]"
        style={{
          border: "2px solid var(--accent)",
          background: "var(--surface)",
          boxShadow: "0 0 0 3px color-mix(in srgb, var(--accent) 13%, transparent)",
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
          style={{ color: "var(--text)" }}
        />
      </span>
      <span
        className="mt-1.5 block font-mono text-[11px] font-medium"
        style={{ color: "var(--subtle)" }}
      >
        was: “{value || "—"}” ·{" "}
        <span style={{ color: "var(--accent)" }}>↵ save</span> · esc cancel
      </span>
    </span>
  );
}

/**
 * Removable chip list with the mock's affordances: × to remove, a brief
 * strikethrough + undo state for a just-removed chip, and a dashed `+ Add`
 * chip that opens an inline input.
 */
export function ChipEditor({
  items,
  onRemove,
  onAdd,
  addLabel = "+ Add skill",
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
    <div className="flex flex-wrap gap-[7px]">
      {items.map((label) => (
        <span
          key={label}
          className="inline-flex items-center gap-1.5 rounded-[7px] border px-2.5 py-1 text-[13px]"
          style={{
            background: "var(--surface-2)",
            borderColor: "var(--border)",
            color: "var(--label)",
          }}
        >
          {label}
          <button
            onClick={() => remove(label)}
            aria-label={`Remove ${label}`}
            className="text-[11px]"
            style={{ color: "var(--subtle)" }}
          >
            ×
          </button>
        </span>
      ))}
      {justRemoved.map((label) => (
        <span
          key={`removed-${label}`}
          className="inline-flex items-center gap-1.5 rounded-[7px] px-2.5 py-1 text-[13px] line-through"
          style={{
            background: "var(--surface)",
            border: "1px dashed var(--danger-border)",
            color: "var(--subtle)",
          }}
        >
          {label}
          <button
            onClick={() => undo(label)}
            className="text-[11px] font-bold no-underline"
            style={{ color: "var(--danger)", textDecoration: "none" }}
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
          className="h-[27px] w-28 rounded-[7px] px-2 text-[13px] outline-none"
          style={{
            background: "var(--surface)",
            border: "2px solid var(--accent)",
            color: "var(--text)",
            boxShadow:
              "0 0 0 3px color-mix(in srgb, var(--accent) 13%, transparent)",
          }}
        />
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="inline-flex items-center gap-1 rounded-[7px] px-[11px] py-1 text-[13px] font-semibold"
          style={{
            background: "var(--surface)",
            border: "1px dashed var(--border-mid)",
            color: "var(--accent)",
          }}
        >
          {addLabel}
        </button>
      )}
    </div>
  );
}
