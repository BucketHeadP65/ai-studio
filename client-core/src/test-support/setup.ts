import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import type { SetupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

// Generalized test lifecycle. `client/`'s setup.ts imported its own MSW server
// directly; here the server instance is passed in by the consuming app so its
// handlers (and the file that constructs them) stay app-local — the discipline
// CLAUDE.md spells out: empty default handlers, every test owns its surface,
// onUnhandledFrame:"error" surfaces a miss as a hard failure.
//
// A consumer's vitest setup file is one line:
//   import { installTestLifecycle } from "client-core/test-support/setup";
//   import { server } from "./msw/server";
//   installTestLifecycle(server);
export function installTestLifecycle(server: SetupServer): void {
  // jsdom doesn't implement scrollIntoView; components using it would crash.
  Element.prototype.scrollIntoView = vi.fn();

  beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
  afterEach(() => {
    cleanup();
    server.resetHandlers();
  });
  afterAll(() => server.close());
}
