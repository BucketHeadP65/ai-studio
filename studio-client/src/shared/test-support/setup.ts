import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

import { server } from "./msw/server";

// The lifecycle every unit test runs under: jest-dom matchers, a scrollIntoView stub, and
// the one MSW server's listen, reset and close. The vitest setup file imports this module.

// jsdom doesn't implement scrollIntoView, so components using it would crash.
Element.prototype.scrollIntoView = vi.fn();

beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());

// MSW answers 500 when a handler throws, so an `expect` inside a handler would fail only the request,
// never the test. Every exception a handler raises fails the test it ran in.
const handlerExceptions: unknown[] = [];
server.events.on("unhandledException", ({ error }) => {
  handlerExceptions.push(error);
});
afterEach(() => {
  const raised = handlerExceptions.splice(0);
  if (raised.length > 0) {
    throw raised[0];
  }
});
