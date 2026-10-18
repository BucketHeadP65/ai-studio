import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Deliberately minimal. The alternative — client-core as a SYMLINKED `file:` dep
// carrying its own node_modules — resolves a SECOND copy of react / react-query /
// next unless every bare specifier is pinned by absolute path. Here .npmrc's
// `install-links=true` installs client-core as a COPY with no nested node_modules,
// so those specifiers already resolve to THIS app's single physical copies — the
// absolute-path alias machinery is unnecessary. So we keep just the `@` alias,
// `dedupe` (a belt-and-braces guard on the single react/etc. copy), jsdom, the
// app-local setup file, and the inline of client-core (it ships raw TS with no
// build step, so it must be transformed inside this app's module graph rather than
// externalised to Node).
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
    dedupe: ["react", "react-dom", "next", "@tanstack/react-query", "vitest"],
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-support/setup.ts"],
    server: {
      deps: {
        inline: [/client-core/],
      },
    },
    include: ["src/**/*.{test,spec}.{ts,tsx}", "tests/integration/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["tests/e2e/**", "node_modules/**"],
    env: {
      NEXT_PUBLIC_KNOWLEDGE_BASE_API_URL: "http://api.test",
      NEXT_PUBLIC_STUDIO_API_URL: "http://studio.test",
    },
  },
});
