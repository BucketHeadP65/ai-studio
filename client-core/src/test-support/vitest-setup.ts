// Vitest setup for client-core's OWN suite. Wires the generalized lifecycle
// helper to client-core's app-local server (see test-server.ts). Consumers do
// the equivalent in their own vitest setup with their own MSW server.
import { installTestLifecycle } from "./setup";
import { server } from "./test-server";

installTestLifecycle(server);
