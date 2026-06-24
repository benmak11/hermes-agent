// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Profile, ProfileResponse } from "@/lib/types";
import { ProfileEditor } from "@/components/ProfileEditor";

export default function OnboardingReviewPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [draft, setDraft] = useState<Profile | null>(null);

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
    onSuccess: () => setSaved(true),
  });

  // After the celebratory "Profile saved" beat, drop the user into the job queue.
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

  return (
    <main className="mx-auto w-full max-w-[760px] flex-1 px-6 py-8">
      {/* step rail */}
      <div className="flex items-center justify-center gap-2 font-mono text-[11px] font-semibold">
        <span className="inline-flex items-center gap-1.5" style={{ color: "var(--good)" }}>
          <span
            className="flex h-[18px] w-[18px] items-center justify-center rounded-full border text-[10px]"
            style={{
              background: "var(--good-bg)",
              borderColor: "var(--good-border)",
              color: "var(--good)",
            }}
          >
            ✓
          </span>
          Upload
        </span>
        <span className="h-px w-[26px]" style={{ background: "var(--text)" }} />
        <span className="inline-flex items-center gap-1.5" style={{ color: "var(--text)" }}>
          <span
            className="flex h-[18px] w-[18px] items-center justify-center rounded-full text-[10px]"
            style={{ background: "var(--text)", color: "var(--surface)" }}
          >
            2
          </span>
          Review
        </span>
      </div>

      <h1
        className="mt-[18px] text-2xl font-semibold tracking-tight"
        style={{ color: "var(--text)" }}
      >
        Here&rsquo;s what Hermes learned
      </h1>
      <p className="mt-2 text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
        Edit anything before we start matching — this is what powers Discovery and
        Matching.
      </p>

      <ProfileEditor value={draft} onChange={setDraft} />

      {save.isError && (
        <p className="mt-4 text-sm" style={{ color: "var(--danger)" }}>
          Could not save: {String(save.error)}
        </p>
      )}

      {/* confirm bar */}
      <div
        className="mt-6 flex items-center justify-between border-t pt-[18px]"
        style={{ borderColor: "var(--divider)" }}
      >
        <span className="text-[13px]" style={{ color: "var(--muted)" }}>
          You can refine this anytime from{" "}
          <span className="font-semibold" style={{ color: "var(--text)" }}>
            Profile
          </span>
          .
        </span>
        <button
          onClick={() => save.mutate(draft)}
          disabled={save.isPending}
          className="h-[42px] rounded-[9px] px-[22px] text-sm font-semibold disabled:opacity-50"
          style={{ background: "var(--text)", color: "var(--surface)" }}
        >
          {save.isPending ? "Saving…" : "Looks good — find me jobs →"}
        </button>
      </div>
    </main>
  );
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
