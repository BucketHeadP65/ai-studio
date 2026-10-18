import type { FileNoteSummary } from "@/api/projects";

// Per-viewer "seen" bookkeeping for the tree's note-status dot: the latest agent-reply
// timestamp this browser has already looked at, for one file in one project. Lives only
// in localStorage -- private to this viewer, never round-tripped through the backend --
// so every access is wrapped in try/catch: a private window, cleared site data, or a
// browser blocking storage must degrade the dot, never throw.

const NAMESPACE = "notes-seen";

function storageKey(projectId: string, path: string): string {
  return `${NAMESPACE}:${projectId}:${path}`;
}

/** The latest agent-reply timestamp this viewer has seen for `path`, or `null` if never recorded or unreadable. */
export function readSeenAt(projectId: string, path: string): string | null {
  try {
    return window.localStorage.getItem(storageKey(projectId, path));
  } catch {
    return null;
  }
}

/** Records `latestAgentReplyAt` as seen for `path`; a `null` timestamp (no agent reply yet) records nothing. */
export function markSeen(projectId: string, path: string, latestAgentReplyAt: string | null): void {
  if (latestAgentReplyAt === null) return;
  try {
    window.localStorage.setItem(storageKey(projectId, path), latestAgentReplyAt);
  } catch {
    // Storage unavailable: the dot falls back to "new" on the next read, never throws here.
  }
}

export type NoteDotTone = "none" | "seen" | "new";

/**
 * The tree row's note-status dot for `summary`: `"none"` with no notes at all, `"seen"` with
 * notes but no agent reply this viewer hasn't already looked at (including a file with no
 * agent reply yet -- nothing to catch up on), `"new"` once an agent reply lands after the
 * viewer's last recorded look.
 */
export function noteDotTone(projectId: string, summary: FileNoteSummary | undefined): NoteDotTone {
  if (!summary) return "none";
  if (summary.open_notes + summary.resolved_notes === 0) return "none";
  if (summary.latest_agent_reply_at === null) return "seen";
  const seenAt = readSeenAt(projectId, summary.path);
  if (seenAt === null) return "new";
  return Date.parse(seenAt) >= Date.parse(summary.latest_agent_reply_at) ? "seen" : "new";
}
