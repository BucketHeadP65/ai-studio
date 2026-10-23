import type { HttpHandler } from "msw";
import { setupServer } from "msw/node";

// The one MSW server for the whole unit suite.
//
// EMPTY default handlers by design: every test owns its network surface via
// `server.use(...)`, and `onUnhandledFrame: "error"` (wired in ../setup.ts) turns any
// unmocked request into a hard failure, so nothing falls through to a real fetch.
export const handlers: HttpHandler[] = [];

export const server = setupServer(...handlers);
