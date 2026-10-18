// The port an end to end run serves on. 3031 by default, which is the always-on dev server's own
// port, so a bare run reuses it. PLAYWRIGHT_PORT gives the run a dev server of its own instead.
//
// A value that is not a port refuses here rather than becoming NaN: a run aimed at
// `http://localhost:NaN` only reports a timeout, which says nothing about what went wrong.

export const DEFAULT_PORT = 3031;

export function resolvePort(raw: string | undefined): number {
  if (raw === undefined || raw === "") return DEFAULT_PORT;
  const port = Number(raw);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`PLAYWRIGHT_PORT must be a port number between 1 and 65535, got ${raw}`);
  }
  return port;
}
