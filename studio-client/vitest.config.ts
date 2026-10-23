import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-support/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}", "tests/integration/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["tests/e2e/**", "node_modules/**"],
    env: {
      NEXT_PUBLIC_KNOWLEDGE_BASE_API_URL: "http://api.test",
      NEXT_PUBLIC_STUDIO_API_URL: "http://studio.test",
    },
  },
});
