// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Profile, ProfileResponse } from "@/lib/types";
import { ProfileEditor } from "@/components/ProfileEditor";
import { TopNav } from "@/components/TopNav";

export default function ProfilePage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [draft, setDraft] = useState<Profile | null>(null);

  useEffect(() => {
    if (!loading && !user) router.push("/login");
  }, [loading, user, router]);

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

  return (
    <>
      <TopNav section="profile" />
      <main className="mx-auto w-full max-w-[760px] flex-1 px-6 py-8">
        {loading || !user || isLoading || !draft ? (
          <div style={{ color: "var(--muted)" }}>Loading…</div>
        ) : error ? (
          <div style={{ color: "var(--danger)" }}>
            Failed to load your profile: {String(error)}
          </div>
        ) : (
          <>
            <div className="flex items-start justify-between gap-4">
              <div>
                <h1
                  className="text-[22px] font-semibold tracking-tight"
                  style={{ color: "var(--text)" }}
                >
                  Your profile
                </h1>
                <p className="mt-2 text-sm" style={{ color: "var(--muted)" }}>
                  The resume-derived profile that fuels Discovery &amp; Matching.
                </p>
              </div>
              <Link
                href="/onboarding"
                className="flex-none rounded-[9px] border px-3.5 py-2 text-[13px] font-semibold"
                style={{
                  background: "var(--surface)",
                  borderColor: "var(--border)",
                  color: "var(--label)",
                }}
              >
                ↑ Re-upload résumé
              </Link>
            </div>

            <ProfileEditor value={draft} onChange={setDraft} />

            <div
              className="mt-6 flex items-center justify-between border-t pt-[18px]"
              style={{ borderColor: "var(--divider)" }}
            >
              <span className="text-[13px]" style={{ color: "var(--muted)" }}>
                {save.isSuccess
                  ? "Saved ✓"
                  : save.isError
                    ? `Could not save: ${String(save.error)}`
                    : "Changes apply to future discovery & matching runs."}
              </span>
              <button
                onClick={() => save.mutate(draft)}
                disabled={save.isPending}
                className="h-[42px] rounded-[9px] px-[22px] text-sm font-semibold disabled:opacity-50"
                style={{ background: "var(--text)", color: "var(--surface)" }}
              >
                {save.isPending ? "Saving…" : "Save changes"}
              </button>
            </div>
          </>
        )}
      </main>
    </>
  );
}
