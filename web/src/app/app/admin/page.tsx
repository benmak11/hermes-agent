// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { AdminAccounts } from "@/components/admin/AdminAccounts";
import { TopNav } from "@/components/TopNav";
import { type Filter, type Roster, adminView, showsAdminNav } from "@/lib/adminAccounts";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/** Operator view of every account. The API answers anyone but the admin with
 *  a 404, which renders as this page's own not-found state. */
export default function AdminPage() {
  const { user, loading } = useAuth();
  const [filter, setFilter] = useState<Filter>("all");

  const { data, error, isPending } = useQuery({
    queryKey: ["admin", "accounts"],
    queryFn: () => apiFetch<Roster>("/admin/accounts"),
    enabled: !!user,
    retry: false,
    // Drop the roster (email addresses) from the cache as soon as the page unmounts.
    gcTime: 0,
  });

  const view = adminView({ loading: loading || !user || isPending, error, data });

  return (
    <>
      {showsAdminNav(view) && <TopNav section="admin" />}
      <AdminAccounts view={view} filter={filter} onFilter={setFilter} />
    </>
  );
}
