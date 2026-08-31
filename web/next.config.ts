import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emit a minimal self-contained server bundle (.next/standalone) for the
  // Docker image — only the runtime files needed by `node server.js`.
  output: "standalone",

  // Google sign-in uses signInWithPopup (src/app/login/page.tsx), which opens a
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
};

export default nextConfig;
