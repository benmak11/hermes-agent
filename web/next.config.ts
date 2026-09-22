import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emit a minimal self-contained server bundle (.next/standalone) for the
  // Docker image — only the runtime files needed by `node server.js`.
  output: "standalone",

  // Google sign-in uses signInWithPopup (src/components/AuthCard.tsx), which opens a
  // popup and then polls `popup.closed` to notice a user who cancelled. That
  // poll needs the opener relationship intact, and Chrome severs it unless this
  // document opts in — which is what the console errors are:
  //
  //     Cross-Origin-Opener-Policy policy would block the window.closed call.
  //
  // The value has to be `same-origin-allow-popups`, not `same-origin`: the
  // latter is stricter and would break the same call this is here to fix.
  // Note this is COOP, not CORS — the API's cross-origin requests are governed
  // separately by WEB_ORIGINS on hermes-api, and those are working.
  headers() {
    return [
      {
        source: "/:path*",
        headers: [
          {
            key: "Cross-Origin-Opener-Policy",
            value: "same-origin-allow-popups",
          },
        ],
      },
    ];
  },

  // Old top-level app paths → /app/* (facelift PR 2). Temporary (307) so nothing
  // caches them if the split ever changes. Query strings pass through unchanged.
  redirects() {
    return [
      { source: "/tracking", destination: "/app/tracking", permanent: false },
      { source: "/profile", destination: "/app/profile", permanent: false },
      { source: "/interviews", destination: "/app/journeys", permanent: false },
      // The interview journal became the journey board (facelift PR 9).
      { source: "/app/interviews", destination: "/app/journeys", permanent: false },
      { source: "/settings/companies", destination: "/app/settings/companies", permanent: false },
      { source: "/applications/:path*", destination: "/app/applications/:path*", permanent: false },
    ];
  },
};

export default nextConfig;
