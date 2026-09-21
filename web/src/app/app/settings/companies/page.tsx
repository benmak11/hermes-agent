// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  CompaniesResponse,
  CompanyActionType,
  CompanyEntry,
} from "@/lib/types";
import { initial } from "@/lib/ui";
import { CompanyTile, tileHue } from "@/components/warm/CompanyTile";
import { Pill } from "@/components/warm/Pill";
import { SERIF } from "@/components/warm/styles";
import { TopNav } from "@/components/TopNav";

type Tab = "unvetted" | "known" | "excluded" | "blocklist";
type Row = {
  platform: string;
  slug: string;
  paused?: boolean;
  excluded?: boolean;
};

export default function CompaniesPage() {
  const { user, loading } = useAuth();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("unvetted");
  const [search, setSearch] = useState("");
  const [showAll, setShowAll] = useState(false);

  const { data } = useQuery({
    queryKey: ["companies"],
    queryFn: () => apiFetch<CompaniesResponse>("/companies"),
    enabled: !!user,
  });

  const action = useMutation({
    mutationFn: (body: {
      platform: string;
      slug: string;
      action: CompanyActionType;
      reason?: string;
    }) =>
      apiFetch("/companies/action", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["companies"] }),
  });

  const flatten = (group?: Record<string, CompanyEntry[]>): Row[] =>
    group
      ? Object.entries(group).flatMap(([platform, rows]) =>
          rows.map((r) => ({
            platform,
            slug: r.slug,
            paused: r.paused,
            excluded: r.excluded,
          })),
        )
      : [];

  const counts = {
    unvetted: flatten(data?.unvetted).length,
    known: flatten(data?.known).length,
    excluded: data?.excluded.length ?? 0,
    blocklist: data?.blocklist.length ?? 0,
  };

  const rows = useMemo(() => {
    const list: Row[] =
      tab === "unvetted"
        ? flatten(data?.unvetted)
        : tab === "known"
          ? flatten(data?.known)
          : // "Excluded" is the user's own overlay, listed from its own array
            // rather than filtered out of the pool: an exclusion can outlive
            // the pool entry it was made against.
            (data?.excluded ?? []).map((e) => ({ ...e, excluded: true }));
    const q = search.trim().toLowerCase();
    return q ? list.filter((r) => r.slug.toLowerCase().includes(q)) : list;
  }, [tab, data, search]);

  if (loading || !user) {
    return <div className="p-8 text-[13.5px]" style={{ color: "var(--ink-4)" }}>Loading…</div>;
  }

  return (
    <>
      <TopNav section="companies" />
      <main className="mx-auto w-full max-w-[900px] flex-1 px-7 py-7">
        <h1
          className="text-[28px] font-normal leading-[1.15]"
          style={{ fontFamily: SERIF, color: "var(--ink)" }}
        >
          Where we look
        </h1>
        <p className="mt-2 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
          Choose which companies we watch for new postings. Block the ones you
          wouldn&apos;t work at.
        </p>

        <div
          className="mt-[18px] inline-flex flex-wrap gap-[3px] rounded-[14px] p-1"
          style={{ background: "#f6ede1" }}
        >
          <TabBtn active={tab === "unvetted"} onClick={() => setTab("unvetted")} label="New finds" count={counts.unvetted} />
          <TabBtn active={tab === "known"} onClick={() => setTab("known")} label="Watching" count={counts.known} />
          <TabBtn active={tab === "excluded"} onClick={() => setTab("excluded")} label="Excluded by me" count={counts.excluded} />
          <TabBtn active={tab === "blocklist"} onClick={() => setTab("blocklist")} label="Blocked for everyone" count={counts.blocklist} />
        </div>

        <p className="mt-2.5 text-[13px]" style={{ color: "var(--ink-4)" }}>
          {tab === "blocklist"
            ? "Blocked for everyone — part of the shared company list, not something you set."
            : tab === "excluded"
              ? "Companies you have excluded. They stay in the shared list; Hermes just stops fetching them for you."
              : "Excluding a company stops future fetches for you only. Jobs already found stay in your list."}
        </p>

        {tab !== "blocklist" && (
          <div className="mt-[18px] flex flex-wrap items-center justify-between gap-3">
            <input
              placeholder="Search companies…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="wm-input h-[38px] w-[250px] rounded-[12px] px-[13px] text-[13px] outline-none"
            />
            <span
              className="text-[11.5px] font-semibold uppercase tracking-[0.06em]"
              style={{ color: "#a3927f" }}
            >
              {Object.keys(
                (tab === "unvetted"
                  ? data?.unvetted
                  : tab === "known"
                    ? data?.known
                    : undefined) ?? {},
              ).join(" · ") || "—"}{" "}
              · {counts[tab]} {tab === "excluded" ? "excluded" : "discovered"}
            </span>
          </div>
        )}

        <div
          className="mt-[14px] overflow-hidden rounded-[18px] border"
          style={{ background: "var(--surface-warm)", borderColor: "var(--border-warm-hair)" }}
        >
          {tab === "blocklist" ? (
            (data?.blocklist.length ?? 0) === 0 ? (
              <p className="p-4 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
                Nothing here yet.
              </p>
            ) : (
              data?.blocklist.map((b) => (
                <div
                  key={`${b.platform}-${b.slug}`}
                  className="flex items-center justify-between border-b px-[18px] py-[13px] text-[14px] last:border-0"
                  style={{ borderColor: "#f4ebdf" }}
                >
                  <span style={{ color: "var(--ink)" }}>
                    {b.slug}{" "}
                    <span style={{ color: "#a3927f" }}>({b.platform})</span>
                  </span>
                  <span className="text-[12.5px]" style={{ color: "var(--ink-4)" }}>
                    {b.reason} · {b.blocked_at}
                  </span>
                </div>
              ))
            )
          ) : rows.length === 0 ? (
            <p className="p-4 text-[13.5px]" style={{ color: "var(--ink-4)" }}>
              {tab === "excluded"
                ? "You haven't excluded any companies."
                : "Nothing here yet."}
            </p>
          ) : (
            (showAll ? rows : rows.slice(0, 30)).map((r) => {
              return (
                <div
                  key={`${r.platform}-${r.slug}`}
                  className="flex flex-wrap items-center justify-between gap-2 border-b px-[18px] py-[13px] last:border-0"
                  style={{ borderColor: "#f4ebdf" }}
                >
                  <div className="flex items-center gap-3">
                    <CompanyTile initial={initial(r.slug)} hue={tileHue(r.slug)} size="sm" />
                    <span
                      className="text-[14px] font-semibold"
                      style={{
                        color:
                          r.paused || r.excluded ? "#b0a08d" : "var(--ink)",
                        textDecoration:
                          r.paused || r.excluded ? "line-through" : "none",
                      }}
                    >
                      {r.slug}
                    </span>
                    <span className="text-[11.5px]" style={{ color: "#a3927f" }}>
                      {r.platform}
                    </span>
                    {r.paused && (
                      <Pill tone="muted">paused for everyone</Pill>
                    )}
                  </div>
                  <div className="flex gap-2">
                    {r.excluded ? (
                      // No un-exclude endpoint exists yet, so this is a state,
                      // not a disabled button pretending to be one.
                      <span className="text-[13px]" style={{ color: "var(--ink-4)" }}>
                        Excluded by you
                      </span>
                    ) : (
                      <>
                        {tab === "unvetted" && (
                          <RowBtn onClick={() => action.mutate({ platform: r.platform, slug: r.slug, action: "dismiss" })}>
                            Dismiss
                          </RowBtn>
                        )}
                        {tab === "known" && (
                          <RowBtn onClick={() => action.mutate({ platform: r.platform, slug: r.slug, action: "pause" })}>
                            Pause
                          </RowBtn>
                        )}
                        <RowBtn danger onClick={() => action.mutate({ platform: r.platform, slug: r.slug, action: "block", reason: "blocked from UI" })}>
                          Block
                        </RowBtn>
                      </>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {tab !== "blocklist" && !showAll && rows.length > 30 && (
          <div
            className="mt-4 text-center text-[13px]"
            style={{ color: "var(--ink-4)" }}
          >
            Showing 30 of {rows.length} ·{" "}
            <button
              onClick={() => setShowAll(true)}
              className="wm-link font-semibold"
            >
              Show all
            </button>
          </div>
        )}
      </main>
    </>
  );
}

function TabBtn({
  active,
  onClick,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
}) {
  return (
    <button
      onClick={onClick}
      className={
        active
          ? "wm-tab wm-tab-active inline-flex items-center gap-[7px] rounded-[11px] px-[15px] py-[7px] text-[13px]"
          : "wm-tab inline-flex items-center gap-[7px] rounded-[11px] px-[15px] py-[7px] text-[13px]"
      }
    >
      {label}
      <span className="text-[11.5px]" style={{ color: "#a3927f" }}>
        {count}
      </span>
    </button>
  );
}

// The `primary` variant went with the Promote button — it had no other caller.
function RowBtn({
  onClick,
  children,
  danger,
  disabled,
}: {
  onClick: () => void;
  children: React.ReactNode;
  danger?: boolean;
  disabled?: boolean;
}) {
  // `.wm-danger` owns every colour of the danger branch (no inline style);
  // the ghost keeps its border colour inline because `.wm-ghost` only owns
  // the background.
  const base = `${danger ? "wm-danger" : "wm-ghost border"} h-[32px] rounded-[10px] px-[13px] text-[12px] font-semibold disabled:opacity-50 disabled:cursor-default`;
  const style: React.CSSProperties | undefined = danger
    ? undefined
    : { borderColor: "#e8dacb", color: "var(--ink-2)" };
  return (
    <button onClick={onClick} disabled={disabled} className={base} style={style}>
      {children}
    </button>
  );
}
