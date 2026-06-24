// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { auth } from "@/lib/firebase";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

/** Fetch the backend, attaching the current user's Firebase ID token. */
export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const user = auth.currentUser;
  const token = user ? await user.getIdToken() : null;

  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers ?? {}),
    },
  });

  if (!res.ok) {
    throw new Error(`${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as T;
}

/**
 * POST multipart/form-data with the Firebase ID token attached. Unlike
 * apiFetch, the browser sets the Content-Type (with the multipart boundary),
 * so we must NOT set it ourselves.
 */
export async function apiUpload<T>(
  path: string,
  body: FormData,
): Promise<T> {
  const user = auth.currentUser;
  const token = user ? await user.getIdToken() : null;

  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    body,
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });

  if (!res.ok) {
    throw new Error(`${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as T;
}
