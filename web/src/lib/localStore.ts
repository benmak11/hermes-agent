// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
"use client";

import { useSyncExternalStore } from "react";

/**
 * Web-storage as a React external store (the sanctioned hydration-safe way to
 * read localStorage/sessionStorage in client components — server snapshots use
 * the fallback, and every write through `writeStored` notifies subscribers).
 */

type Area = "local" | "session";

const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  window.addEventListener("storage", cb);
  return () => {
    listeners.delete(cb);
    window.removeEventListener("storage", cb);
  };
}

function storage(area: Area): Storage {
  return area === "local" ? localStorage : sessionStorage;
}

export function readStored(area: Area, key: string): string | null {
  try {
    return storage(area).getItem(key);
  } catch {
    return null;
  }
}

export function writeStored(area: Area, key: string, value: string | null): void {
  try {
    if (value === null) storage(area).removeItem(key);
    else storage(area).setItem(key, value);
  } catch {
    // Storage blocked/full — reads will keep returning the previous value.
  }
  emit();
}

// getSnapshot must be referentially stable for unchanged data, so parsed
// values are cached per key and invalidated by the raw string.
const cache = new Map<string, { raw: string | null; value: unknown }>();

/**
 * Subscribe to a stored value. `key: null` returns `fallback` (e.g. while the
 * uid isn't known yet). `parse` must be pure; `fallback` must be referentially
 * stable (a module-level constant).
 */
export function useStored<T>(
  area: Area,
  key: string | null,
  parse: (raw: string | null) => T,
  fallback: T,
): T {
  return useSyncExternalStore(
    subscribe,
    () => {
      if (key === null) return fallback;
      const cacheKey = `${area}:${key}`;
      const raw = readStored(area, key);
      const hit = cache.get(cacheKey);
      if (hit && hit.raw === raw) return hit.value as T;
      const value = parse(raw);
      cache.set(cacheKey, { raw, value });
      return value;
    },
    () => fallback,
  );
}
