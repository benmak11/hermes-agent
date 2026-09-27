// Copyright (c) 2026 Baynham Makusha. All rights reserved.
// Unauthorized copying, distribution, or use is prohibited.

/**
 * Error thrown by the API helpers. Carries the HTTP status and the
 * `X-Request-Id` so a failure surfaced in the UI is traceable to the backend.
 * Subclasses Error, so existing `String(err)` / `err.message` callers keep
 * working — the id is embedded in the message.
 *
 * **Its own module, away from `api.ts`.** That module initialises Firebase at
 * import time, so anything that merely wants to *inspect* an error — the
 * spend-consent 402 parser, and its tests — would otherwise have to boot auth
 * to do it. `api.ts` re-exports this, so every existing import still works.
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
