// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api";
import { legacyKey, legacyToJourney, parseLegacy } from "@/lib/journeysDerive";
import { readStored, writeStored } from "@/lib/localStore";
import type { Journey, JourneyInput } from "@/lib/types";

/**
 * Journeys live server-side (`users/{uid}/journeys`) as whole documents the
 * client owns: every edit is a full PUT and the last write wins. The hooks
 * here are the only network surface; the derivations in `journeysDerive` are
 * pure and the page composes the two.
 */

export const JOURNEYS_KEY = ["journeys"] as const;

type ListResponse = { journeys: Journey[] };

export function useJourneys(enabled: boolean) {
  return useQuery({
    queryKey: JOURNEYS_KEY,
    queryFn: () => apiFetch<ListResponse>("/journeys"),
    enabled,
  });
}

export function useJourneyMutations() {
  const queryClient = useQueryClient();
  const onSuccess = () => queryClient.invalidateQueries({ queryKey: JOURNEYS_KEY });
  const create = useMutation({
    mutationFn: (input: JourneyInput) =>
      apiFetch<Journey>("/journeys", { method: "POST", body: JSON.stringify(input) }),
    onSuccess,
  });
  const save = useMutation({
    mutationFn: (j: Journey) =>
      apiFetch<Journey>(`/journeys/${j.id}`, { method: "PUT", body: JSON.stringify(j) }),
    onSuccess,
  });
  const remove = useMutation({
    mutationFn: (id: string) => apiFetch<{ ok: true }>(`/journeys/${id}`, { method: "DELETE" }),
    onSuccess,
  });
  return { create, save, remove };
}

/**
 * One-time move of the pre-PR-9 localStorage interview journal onto the
 * server. Only called after a *successful* empty `GET /journeys` — an error
 * (including the rolling-deploy 404 while the old api is still up) leaves the
 * key untouched. The key is removed before the first POST so a second tab of
 * the same browser finds nothing; the server count guards the cross-browser
 * case. On a mid-way failure the entries not yet posted are written back so
 * the next load finishes the job.
 *
 * Returns how many entries were imported; the caller invalidates the list.
 */
export async function importLegacyJournal(uid: string, serverCount: number): Promise<number> {
  if (serverCount > 0) return 0;
  const key = legacyKey(uid);
  const entries = parseLegacy(readStored("local", key));
  if (entries.length === 0) return 0;
  writeStored("local", key, null);
  const nowIso = new Date().toISOString();
  for (let i = 0; i < entries.length; i++) {
    try {
      await apiFetch<Journey>("/journeys", {
        method: "POST",
        body: JSON.stringify(legacyToJourney(entries[i], nowIso)),
      });
    } catch (err) {
      writeStored("local", key, JSON.stringify(entries.slice(i)));
      console.warn("journeys.import.partial", err);
      throw err;
    }
  }
  return entries.length;
}
