// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AdminAccounts, type SeatControls } from "@/components/admin/AdminAccounts";
import { TopNav } from "@/components/TopNav";
import {
  type Filter,
  type GrantResponse,
  type RevokeResponse,
  type Roster,
  type SeatPanel,
  adminView,
  seatError,
  showsAdminNav,
} from "@/lib/adminAccounts";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/** Operator view of every account, with seat grant and revoke. The API answers
 *  anyone but the admin with a 404, which renders as this page's own
 *  not-found state. */
export default function AdminPage() {
  const { user, loading } = useAuth();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<Filter>("all");
  const [panel, setPanel] = useState<SeatPanel | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const { data, error, isPending } = useQuery({
    queryKey: ["admin", "accounts"],
    queryFn: () => apiFetch<Roster>("/admin/accounts"),
    enabled: !!user,
    retry: false,
    // Drop the roster (email addresses) from the cache as soon as the page unmounts.
    gcTime: 0,
  });

  const done = (message: string) => {
    setPanel(null);
    setNotice(message);
    queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
  };

  const grant = useMutation({
    mutationFn: (p: { email: string; note: string }) =>
      apiFetch<GrantResponse>("/admin/seats", {
        method: "POST",
        body: JSON.stringify({ email: p.email, note: p.note.trim() || null }),
      }),
    onSuccess: (res) => done(`Seat granted to ${res.email}.`),
  });

  const revoke = useMutation({
    mutationFn: (p: { email: string; confirm: string }) =>
      apiFetch<RevokeResponse>("/admin/seats/revoke", {
        method: "POST",
        body: JSON.stringify(p),
      }),
    onSuccess: (res) => done(res.message),
  });

  const active = panel?.kind === "revoke" ? revoke : grant;

  const seats: SeatControls | undefined = user
    ? {
        me: { uid: user.uid, email: user.email },
        panel,
        pending: grant.isPending || revoke.isPending,
        error: panel && active.isError ? seatError(active.error) : null,
        notice,
        onPanel: (next) => {
          // A fresh dialog starts without the last attempt's error.
          if (next?.row.key !== panel?.row.key || next?.kind !== panel?.kind) {
            grant.reset();
            revoke.reset();
          }
          setPanel(next);
        },
        onSubmit: () => {
          if (!panel?.row.email) return;
          setNotice(null);
          if (panel.kind === "grant") {
            grant.mutate({ email: panel.row.email, note: panel.note });
          } else {
            revoke.mutate({ email: panel.row.email, confirm: panel.typed });
          }
        },
      }
    : undefined;

  const view = adminView({ loading: loading || !user || isPending, error, data });

  return (
    <>
      {showsAdminNav(view) && <TopNav section="admin" />}
      <AdminAccounts view={view} filter={filter} onFilter={setFilter} seats={seats} />
    </>
  );
}
