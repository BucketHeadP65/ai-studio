import { z } from "zod";

import { STUDIO_API_URL } from "./studio-config";
import { type ChatReply, ChatReplySchema } from "./studio-types";

// The streaming half of the authoring conversation. POST /api/sessions/{id}/messages
// takes pydantic-ai's own submit-message body (the new message alone, the history held
// by the server) and answers the Vercel AI data-stream protocol (v6): one JSON chunk
// per SSE data line, closed by `data: [DONE]`. The chunks this client renders:
// text-delta (assistant text as it generates), tool-input-start (a named tool act),
// data-settle (the settled turn, zod-parsed here), and error (a sentence that ends the
// turn). Everything else in the protocol is passed over; the settled payload is the one
// truth, and it is read to the end of the stream, so a chunk that follows it still lands.

const ChunkSchema = z.looseObject({ type: z.string() });
const TextDeltaSchema = z.object({ delta: z.string() });
const ToolStartSchema = z.object({ toolName: z.string() });
const SettleSchema = z.object({ data: ChatReplySchema });
const ErrorChunkSchema = z.object({ errorText: z.string() });
const FailureSchema = z.object({ detail: z.string() });

export interface StreamHandlers {
  onDelta: (text: string) => void;
  onStatus: (note: string) => void;
}

export class ChatStreamError extends Error {}

// The SSE line splitter both this stream and the step chat's own stream (`step-chat.ts`) use:
// a buffer of raw bytes decoded so far, split into whole `\n\n`-terminated frames, each frame's
// own `data:` line unwrapped, the incomplete tail handed back to prefix the next read.
export function dataLines(buffer: string): { complete: string[]; rest: string } {
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  const complete: string[] = [];
  for (const frame of parts) {
    for (const line of frame.split("\n")) {
      if (line.startsWith("data:")) {
        complete.push(line.slice("data:".length).trimStart());
      }
    }
  }
  return { complete, rest };
}

export async function chatStream(
  sessionId: string,
  message: string,
  handlers: StreamHandlers,
): Promise<ChatReply> {
  const response = await fetch(
    `${STUDIO_API_URL}/api/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      // `id` is the protocol's chat id, which the server reads as the conversation this turn
      // belongs to, so it is the session's own id. The id beside it names the message.
      body: JSON.stringify({
        trigger: "submit-message",
        id: sessionId,
        messages: [
          { id: crypto.randomUUID(), role: "user", parts: [{ type: "text", text: message }] },
        ],
      }),
    },
  );
  if (!response.ok || response.body === null) {
    const body = await response.text();
    try {
      const parsed = FailureSchema.safeParse(JSON.parse(body));
      throw new ChatStreamError(parsed.success ? parsed.data.detail : body);
    } catch (error) {
      if (error instanceof ChatStreamError) {
        throw error;
      }
      throw new ChatStreamError(body || `the stream answered ${response.status}`);
    }
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let settled: ChatReply | null = null;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const { complete, rest } = dataLines(buffer);
    buffer = rest;
    for (const data of complete) {
      if (data === "[DONE]") {
        continue;
      }
      const chunk = ChunkSchema.parse(JSON.parse(data));
      if (chunk.type === "text-delta") {
        handlers.onDelta(TextDeltaSchema.parse(chunk).delta);
      } else if (chunk.type === "tool-input-start") {
        handlers.onStatus(`running ${ToolStartSchema.parse(chunk).toolName}`);
      } else if (chunk.type === "data-settle") {
        settled = SettleSchema.parse(chunk).data;
      } else if (chunk.type === "error") {
        throw new ChatStreamError(ErrorChunkSchema.parse(chunk).errorText);
      }
    }
  }

  if (settled === null) {
    throw new ChatStreamError("the stream closed without settling the turn");
  }
  return settled;
}
