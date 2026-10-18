import type { HttpHandler } from "msw";
import { setupServer } from "msw/node";

// The app-local MSW server for studio-client's suite.
//
// EMPTY default handlers by design: every test owns its network surface via
// `server.use(...)`, and `onUnhandledFrame: "error"` (wired by
// installTestLifecycle in setup.ts) turns any unmocked request into a hard
// failure — no silent fallthrough to a real fetch. The handlers + the file that
// constructs them stay app-local (the CLAUDE.md discipline); client-core only
// supplies the generalized listen/reset/close lifecycle.
export const handlers: HttpHandler[] = [];

export const server = setupServer(...handlers);
