// MSW server for client-core's OWN unit suite.
//
// The shared test-support harness (installTestLifecycle) deliberately takes the
// server instance from the consuming app — msw handlers stay app-local (the
// CLAUDE.md discipline). This module is that "app-local" server, but for
// client-core's own tests: an empty default handler list so every test owns its
// network surface via `server.use(...)`, with onUnhandledFrame:"error"
// (wired in vitest-setup.ts) turning any miss into a hard failure.
import type { HttpHandler } from "msw";
import { setupServer } from "msw/node";

export const handlers: HttpHandler[] = [];

export const server = setupServer(...handlers);
