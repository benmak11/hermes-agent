// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type {
  CompaniesResponse,
  CompanyActionType,
  CompanyEntry,
} from "@/lib/types";
import { avatarColor, initial } from "@/lib/ui";
import { TopNav } from "@/components/TopNav";

type Tab = "unvetted" | "known" | "blocklist";
type Row = { platform: string; slug: string; paused?: boolean };

export default function CompaniesPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("unvetted");
  const [search, setSearch] = useState("");

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

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
          rows.map((r) => ({ platform, slug: r.slug, paused: r.paused })),
        )
      : [];

  const counts = {
    unvetted: flatten(data?.unvetted).length,
    known: flatten(data?.known).length,
    blocklist: data?.blocklist.length ?? 0,
  };

  const rows = useMemo(() => {
    const list =
      tab === "unvetted" ? flatten(data?.unvetted) : flatten(data?.known);
    const q = search.trim().toLowerCase();
    return q ? list.filter((r) => r.slug.toLowerCase().includes(q)) : list;
  }, [tab, data, search]);

  if (loading || !user) {
    return <div className="p-8" style={{ color: "var(--muted)" }}>Loading…</div>;
  }

  return (
    <>
      <TopNav section="companies" />
      <main className="mx-auto w-full max-w-[880px] flex-1 px-8 py-7">
        <h1
          className="text-[22px] font-semibold tracking-tight"
          style={{ color: "var(--text)" }}
        >
          Companies
        </h1>
        <p className="mt-[7px] text-[13px]" style={{ color: "var(--muted)" }}>
          Control which companies Hermes scrapes for new postings.
        </p>

        <div
          className="mt-[18px] inline-flex gap-0.5 rounded-[10px] p-[3px]"
          style={{ background: "var(--surface-2)" }}
        >
          <TabBtn active={tab === "unvetted"} onClick={() => setTab("unvetted")} label="Unvetted" count={counts.unvetted} />
          <TabBtn active={tab === "known"} onClick={() => setTab("known")} label="Known" count={counts.known} />
          <TabBtn active={tab === "blocklist"} onClick={() => setTab("blocklist")} label="Blocklist" count={counts.blocklist} />
        </div>

        {tab !== "blocklist" && (
          <div className="mt-[18px]">
            <input
              placeholder="Search companies…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-9 w-[260px] rounded-[9px] border px-3 text-[13px] outline-none"
              style={{
                background: "var(--surface)",
                borderColor: "var(--border)",
                color: "var(--text)",
              }}
            />
          </div>
        )}

        <div
          className="mt-3.5 overflow-hidden rounded-xl border"
          style={{ background: "var(--surface)", borderColor: "var(--border)" }}
        >
          {tab === "blocklist" ? (
            (data?.blocklist.length ?? 0) === 0 ? (
              <p className="p-3 text-sm" style={{ color: "var(--muted)" }}>
                Empty.
              </p>
            ) : (
              data?.blocklist.map((b) => (
                <div
                  key={`${b.platform}-${b.slug}`}
                  className="flex items-center justify-between border-b p-3 text-sm last:border-0"
                  style={{ borderColor: "var(--divider)" }}
                >
                  <span style={{ color: "var(--text)" }}>
                    {b.slug}{" "}
                    <span style={{ color: "var(--subtle)" }}>({b.platform})</span>
                  </span>
                  <span className="text-[13px]" style={{ color: "var(--muted)" }}>
                    {b.reason} · {b.blocked_at}
                  </span>
                </div>
              ))
            )
          ) : rows.length === 0 ? (
            <p className="p-3 text-sm" style={{ color: "var(--muted)" }}>
              No companies.
            </p>
          ) : (
            rows.map((r) => {
              const av = avatarColor(r.slug);
              return (
                <div
                  key={`${r.platform}-${r.slug}`}
                  className="flex items-center justify-between border-b px-4 py-[11px] last:border-0"
                  style={{ borderColor: "var(--divider)" }}
                >
                  <div className="flex items-center gap-[11px]">
                    <span
                      className="flex h-7 w-7 items-center justify-center rounded-[7px] text-xs font-bold"
                      style={{ background: av.bg, color: av.color }}
                    >
                      {initial(r.slug)}
                    </span>
                    <span
                      className="text-sm font-medium"
                      style={{
                        color: r.paused ? "var(--subtle)" : "var(--text)",
                        textDecoration: r.paused ? "line-through" : "none",
                      }}
                    >
                      {r.slug}
                    </span>
                    <span className="font-mono text-[11px]" style={{ color: "var(--subtle)" }}>
                      {r.platform}
                    </span>
                  </div>
                  <div className="flex gap-2">
                    {tab === "unvetted" ? (
                      <>
                        <RowBtn primary onClick={() => action.mutate({ platform: r.platform, slug: r.slug, action: "promote" })}>
                          Promote
                        </RowBtn>
                        <RowBtn onClick={() => action.mutate({ platform: r.platform, slug: r.slug, action: "dismiss" })}>
                          Dismiss
                        </RowBtn>
                        <RowBtn danger onClick={() => action.mutate({ platform: r.platform, slug: r.slug, action: "block", reason: "blocked from UI" })}>
                          Block
                        </RowBtn>
                      </>
                    ) : (
                      <>
                        <RowBtn
                          onClick={() => action.mutate({ platform: r.platform, slug: r.slug, action: "pause" })}
                          disabled={r.paused}
                        >
                          {r.paused ? "Paused" : "Pause"}
                        </RowBtn>
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
      className="inline-flex items-center gap-[7px] rounded-[7px] px-[13px] py-1.5 text-[13px]"
      style={{
        background: active ? "var(--surface)" : "transparent",
        color: active ? "var(--text)" : "var(--label)",
        fontWeight: active ? 600 : 500,
        boxShadow: active ? "0 1px 2px rgba(0,0,0,0.06)" : "none",
      }}
    >
      {label}
      <span className="font-mono text-[11px] font-semibold" style={{ color: "var(--subtle)" }}>
        {count}
      </span>
    </button>
  );
}

function RowBtn({
  onClick,
  children,
  primary,
  danger,
  disabled,
}: {
  onClick: () => void;
  children: React.ReactNode;
  primary?: boolean;
  danger?: boolean;
  disabled?: boolean;
}) {
  const base =
    "h-[30px] rounded-[7px] px-[13px] text-xs disabled:opacity-50 disabled:cursor-default";
  const style: React.CSSProperties = primary
    ? { background: "var(--text)", color: "var(--surface)", fontWeight: 600 }
    : danger
      ? {
          background: "var(--surface)",
          border: "1px solid var(--danger-border)",
          color: "var(--danger)",
          fontWeight: 500,
        }
      : {
          background: "var(--surface)",
          border: "1px solid var(--border)",
          color: "var(--label)",
          fontWeight: 500,
        };
  return (
    <button onClick={onClick} disabled={disabled} className={base} style={style}>
      {children}
    </button>
  );
}
