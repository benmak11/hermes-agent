import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emit a minimal self-contained server bundle (.next/standalone) for the
  // Docker image — only the runtime files needed by `node server.js`.
  output: "standalone",
};

export default nextConfig;
