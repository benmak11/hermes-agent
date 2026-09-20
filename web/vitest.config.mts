import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Pure unit tests only: node environment, no DOM. Components under test render
// through react-dom/server, so nothing here needs jsdom or a React plugin.
// `.mts` because package.json is not "type": "module" (Next's vitest guide).
// vitest's transformer (oxc) defaults to the automatic JSX runtime, matching
// tsconfig's `jsx: "react-jsx"`, so .tsx test subjects compile as-is.
export default defineConfig({
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
