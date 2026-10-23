// The terminal pane's wire framing, pure and DOM-free so it is unit-testable without a real
// xterm mount (xterm needs a real browser layout — matchMedia, canvas — that jsdom does not
// provide; see graph-model.ts for the same split against sigma's WebGL mount). Mirrors the
// contract in studio/agent/features/terminal/routes.py's module docstring exactly: binary frames
// carry raw pty bytes both ways; a text frame is JSON control, one shape today — a resize, both
// dimensions 1-65535. The server drops (not fails) any frame outside that shape or range, so a
// stale/oversized resize this module builds is silently ignored rather than killing the session —
// nothing here needs to pre-validate what the server already refuses safely.

export interface ResizeMessage {
  type: "resize";
  cols: number;
  rows: number;
}

export function resizeMessage(cols: number, rows: number): string {
  return JSON.stringify({ type: "resize", cols, rows } satisfies ResizeMessage);
}

// `baseHttpUrl` is the agent server's own base (e.g. STUDIO_API_URL, :8770) — the terminal
// speaks raw WS on that same host, never the shared fetch client.
export function terminalWsUrl(baseHttpUrl: string, cwd: string): string {
  const wsBase = baseHttpUrl.replace(/^http/, "ws");
  return `${wsBase}/api/terminal/ws?cwd=${encodeURIComponent(cwd)}`;
}
