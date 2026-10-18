import type { UIMessage } from "ai";

import { parseTranscriptPayload, transcriptTurns } from "@/api/sessions";
import type { TranscriptMessage } from "@/api/sessions-types";

// Where a bubble stands in for a run the agent itself started: scans the stored transcript's own
// tool calls to the run tool, turn by turn, so an info-box entry always lands after that turn's own
// last rendered bubble, whichever run of the turn made the call. A tool call still only live on
// the wire, not yet in the stored transcript, carries no entry yet.

export const RUN_TOOL_NAME = "run_workflow";

export interface RunToolCallEntry {
  afterMessageId: string;
  workflowId: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function workflowIdFromArgs(value: unknown): string | null {
  if (typeof value === "string") {
    let parsed: unknown;
    try {
      parsed = JSON.parse(value);
    } catch {
      return null;
    }
    return workflowIdFromArgs(parsed);
  }
  if (isRecord(value) && typeof value.workflow_id === "string") {
    return value.workflow_id;
  }
  return null;
}

// A message id is `${run_id}:${idx}`; the run id itself never contains a colon (it's a UUID), so
// the last colon in the id always separates the two.
function runIdOf(messageId: string): string | null {
  const lastColon = messageId.lastIndexOf(":");
  return lastColon === -1 ? null : messageId.slice(0, lastColon);
}

function workflowIdsOf(rows: TranscriptMessage[]): string[] {
  const workflowIds: string[] = [];
  for (const row of rows) {
    if (row.kind !== "response") continue;
    const payload = parseTranscriptPayload(row.payload);
    if (payload === null) continue;
    for (const part of payload.parts) {
      if (part.part_kind !== "tool-call" || part.tool_name !== RUN_TOOL_NAME) continue;
      const workflowId = workflowIdFromArgs(part.args);
      if (workflowId !== null) workflowIds.push(workflowId);
    }
  }
  return workflowIds;
}

export function runToolCallEntries(
  messages: UIMessage[],
  transcript: TranscriptMessage[],
): RunToolCallEntry[] {
  const turns = transcriptTurns(transcript);
  const turnOfRunId = new Map<string, number>();
  const workflowIdsByTurn = new Map<number, string[]>();
  turns.forEach((turn, turnIndex) => {
    for (const row of turn.rows) {
      if (!turnOfRunId.has(row.run_id)) turnOfRunId.set(row.run_id, turnIndex);
    }
    const workflowIds = workflowIdsOf(turn.rows);
    if (workflowIds.length > 0) workflowIdsByTurn.set(turnIndex, workflowIds);
  });
  if (workflowIdsByTurn.size === 0) return [];

  const lastIndexForTurn = new Map<number, number>();
  messages.forEach((message, index) => {
    const runId = runIdOf(message.id);
    const turnIndex = runId === null ? undefined : turnOfRunId.get(runId);
    if (turnIndex !== undefined) lastIndexForTurn.set(turnIndex, index);
  });

  const entries: RunToolCallEntry[] = [];
  for (const [turnIndex, workflowIds] of workflowIdsByTurn) {
    const index = lastIndexForTurn.get(turnIndex);
    const message = index === undefined ? undefined : messages[index];
    if (message === undefined) continue;
    for (const workflowId of workflowIds) {
      entries.push({ afterMessageId: message.id, workflowId });
    }
  }
  return entries;
}
