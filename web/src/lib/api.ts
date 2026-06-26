// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.
import { auth } from "@/lib/firebase";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8080";

/**
 * Correlation id for a single backend call, sent as the `X-Request-Id` header.
 * The API adopts an inbound id (see RequestContextMiddleware), so the same id
 * ties a request the user made in the browser to its server logs and Cloud
 * Trace span — paste it into Cloud Logging as `jsonPayload.request_id="..."`.
 */
export function newRequestId(): string {
  return crypto.randomUUID();
}

/**
 * Error thrown by the API helpers. Carries the HTTP status and the
 * `X-Request-Id` so a failure surfaced in the UI is traceable to the backend.
 * Subclasses Error, so existing `String(err)` / `err.message` callers keep
 * working — the id is embedded in the message.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly requestId: string;
  readonly body: string;

  constructor(status: number, body: string, requestId: string) {
    super(`${status}: ${body} (request ${requestId})`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.requestId = requestId;
  }
}

/** Run the request, raising an ApiError (with the correlation id) on failure. */
async function send<T>(
  path: string,
  init: RequestInit,
  requestId: string,
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  // The API echoes X-Request-Id; cross-origin it may be hidden by CORS, so fall
  // back to the id we sent (which the server adopted either way).
  const id = res.headers.get("x-request-id") ?? requestId;

  if (!res.ok) {
    const body = await res.text();
    console.error(`api ${res.status} ${path} (request ${id})`, body);
    throw new ApiError(res.status, body, id);
  }
  return (await res.json()) as T;
}

/** Fetch the backend, attaching the current user's Firebase ID token. */
export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const user = auth.currentUser;
  const token = user ? await user.getIdToken() : null;
  const requestId = newRequestId();

  return send<T>(
    path,
    {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Request-Id": requestId,
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.headers ?? {}),
      },
    },
    requestId,
  );
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
  const requestId = newRequestId();

  return send<T>(
    path,
    {
      method: "POST",
      body,
      headers: {
        "X-Request-Id": requestId,
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    },
    requestId,
  );
}
