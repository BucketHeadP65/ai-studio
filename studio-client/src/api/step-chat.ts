"use client";

import { useEffect, useState } from "react";
import { z } from "zod";

import { ApiError, createApiClient } from "client-core/api/client";

import { dataLines } from "./chat-stream";
import { NEVER_ANSWERED } from "./sessions";
import { STUDIO_API_URL } from "./studio-config";

import type { MessagePayload } from "./sessions-types";
import type { UIMessage } from "ai";

// The step chat's own wire: a session-scoped listing of every open agentic-step line, a
// replayable SSE tail of one line's Vercel v6 UI-message stream, and a steer POST. The stream
// speaks the same protocol the session chat's own stream does (`pydantic_ai.ui.vercel_ai`,
// sdk_version 6): each frame is already `data: `-framed by the encoder, closed by `data:
// [DONE]`, plus the line's own `data-steer` frame carrying a person's mid-step words.

export const StepLineSchema = z.object({
  node: z.string(),
  item: z.string(),
  executing: z.boolean(),
  settled: z.boolean(),
});
export type StepLine = z.infer<typeof StepLineSchema>;

const StepLinesSchema = z.object({ steps: z.array(StepLineSchema) });

const { request } = createApiClient({ baseUrl: STUDIO_API_URL });

// A steer posted to a line that is not executing (finished, or waiting between turns).
export class StepNotSteerableError extends Error {}

// The steer route's own failure shape (`routes.py`'s `session_steer_step` and every
// `_no_such_session`-style refusal in this app): `{"ok": false, "message": "..."}`, never
// `{"detail": ...}` -- that shape belongs to a different route entirely.
const FailureSchema = z.object({ ok: z.boolean(), message: z.string() });

function stepNotSteerableMessage(error: ApiError): string {
  try {
    const parsed = FailureSchema.safeParse(JSON.parse(error.message));
    return parsed.success ? parsed.data.message : error.message;
  } catch {
    return error.message;
  }
}

export const stepChatApi = {
  lines: (sessionId: string): Promise<StepLine[]> =>
    request<z.infer<typeof StepLinesSchema>>(
      `/api/sessions/${encodeURIComponent(sessionId)}/run/steps`,
      StepLinesSchema,
    ).then((result) => result.steps),

  steer: async (sessionId: string, node: string, item: string, message: string): Promise<void> => {
    const params = new URLSearchParams({ item });
    try {
      await request<{ ok: true }>(
        `/api/sessions/${encodeURIComponent(sessionId)}/run/steps/${encodeURIComponent(node)}/steer?${params.toString()}`,
        z.object({ ok: z.literal(true) }),
        { method: "POST", body: JSON.stringify({ message }) },
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        throw new StepNotSteerableError(stepNotSteerableMessage(error));
      }
      throw error;
    }
  },
};

// ---- the live stream: raw v6 chunks -> the UIMessage shape Conversation/AssistantTurn render ----

type Part = UIMessage["parts"][number];

interface Builder {
  messages: UIMessage[];
  current: UIMessage | null;
  textAt: Map<string, number>;
  reasoningAt: Map<string, number>;
  toolAt: Map<string, number>;
  toolNameOf: Map<string, string>;
  toolInputOf: Map<string, unknown>;
  seq: number;
}

function newBuilder(): Builder {
  return {
    messages: [],
    current: null,
    textAt: new Map(),
    reasoningAt: new Map(),
    toolAt: new Map(),
    toolNameOf: new Map(),
    toolInputOf: new Map(),
    seq: 0,
  };
}

function freshId(builder: Builder, prefix: string): string {
  builder.seq += 1;
  return `line-${prefix}-${builder.seq}`;
}

// Opens the message the stream is currently filling, creating one only when none is open --
// a `start` chunk names it explicitly, everything else opens one defensively so a frame that
// arrives before its `start` (or after a reconnect mid-message) still renders.
function openMessage(builder: Builder, id?: string): UIMessage {
  if (builder.current === null) {
    builder.current = { id: id ?? freshId(builder, "assistant"), role: "assistant", parts: [] };
    builder.textAt = new Map();
    builder.reasoningAt = new Map();
    builder.toolAt = new Map();
  }
  return builder.current;
}

// Finalizes the open message into the list -- a `finish` chunk closes it in the ordinary case,
// a `data-steer` closes it early so the person's words land between whatever came before and
// whatever the step says next.
function closeMessage(builder: Builder): void {
  if (builder.current !== null && builder.current.parts.length > 0) {
    builder.messages = [...builder.messages, builder.current];
  }
  builder.current = null;
}

function appendPart(builder: Builder, part: Part): number {
  const current = openMessage(builder);
  const index = current.parts.length;
  builder.current = { ...current, parts: [...current.parts, part] };
  return index;
}

function replacePart(builder: Builder, index: number, part: Part): void {
  if (builder.current === null) {
    return;
  }
  const parts = builder.current.parts.slice();
  parts[index] = part;
  builder.current = { ...builder.current, parts };
}

function snapshot(builder: Builder): UIMessage[] {
  return builder.current !== null && builder.current.parts.length > 0
    ? [...builder.messages, builder.current]
    : builder.messages;
}

const ChunkSchema = z.looseObject({ type: z.string() });
const StartChunkSchema = z.object({ messageId: z.string().optional() });
const TextEventSchema = z.object({ id: z.string() });
const TextDeltaEventSchema = z.object({ id: z.string(), delta: z.string() });
const ToolInputStartEventSchema = z.object({ toolCallId: z.string(), toolName: z.string() });
const ToolInputAvailableEventSchema = z.object({ toolCallId: z.string(), toolName: z.string(), input: z.unknown() });
const ToolOutputAvailableEventSchema = z.object({ toolCallId: z.string(), output: z.unknown() });
const ToolOutputErrorEventSchema = z.object({ toolCallId: z.string(), errorText: z.string() });
const DataEventSchema = z.object({ id: z.string().optional(), data: z.unknown() });
const SteerDataSchema = z.object({ message: z.string(), at: z.string().datetime({ offset: true }) });

// The stream's own opening frame: the absolute index the chunks that follow start counting
// from, and what the browser had asked to resume from. `floor > requested` means the live
// buffer had already trimmed past that point -- the browser is caught up from `floor` on, but
// missed whatever sat between the two.
const ReplayFloorChunkSchema = z.object({
  type: z.literal("data-replay-floor"),
  data: z.object({ floor: z.number(), requested: z.number() }),
});

// One raw, already-JSON-parsed v6 chunk folded onto the builder. Unrecognized chunk kinds
// (error, abort, message-metadata, source-*, file, tool-input-delta, tool-input-error,
// tool-approval-*, tool-output-denied) carry nothing this surface renders and pass over.
function applyChunk(builder: Builder, raw: unknown): void {
  const chunk = ChunkSchema.parse(raw);
  switch (chunk.type) {
    case "start": {
      const { messageId } = StartChunkSchema.parse(chunk);
      openMessage(builder, messageId);
      return;
    }
    case "finish": {
      closeMessage(builder);
      return;
    }
    case "start-step": {
      appendPart(builder, { type: "step-start" });
      return;
    }
    case "finish-step": {
      return;
    }
    case "text-start": {
      const { id } = TextEventSchema.parse(chunk);
      // `appendPart` may open the message and reset `textAt` to a fresh map, so the index
      // is recorded on it AFTER that call, not chained into the same expression.
      const index = appendPart(builder, { type: "text", text: "", state: "streaming" });
      builder.textAt.set(id, index);
      return;
    }
    case "text-delta": {
      const { id, delta } = TextDeltaEventSchema.parse(chunk);
      const index = builder.textAt.get(id);
      const part = index === undefined ? undefined : builder.current?.parts[index];
      if (index !== undefined && part !== undefined && part.type === "text") {
        replacePart(builder, index, { ...part, text: part.text + delta });
      }
      return;
    }
    case "text-end": {
      const { id } = TextEventSchema.parse(chunk);
      const index = builder.textAt.get(id);
      const part = index === undefined ? undefined : builder.current?.parts[index];
      if (index !== undefined && part !== undefined && part.type === "text") {
        replacePart(builder, index, { ...part, state: "done" });
      }
      return;
    }
    case "reasoning-start": {
      const { id } = TextEventSchema.parse(chunk);
      const index = appendPart(builder, { type: "reasoning", text: "", state: "streaming" });
      builder.reasoningAt.set(id, index);
      return;
    }
    case "reasoning-delta": {
      const { id, delta } = TextDeltaEventSchema.parse(chunk);
      const index = builder.reasoningAt.get(id);
      const part = index === undefined ? undefined : builder.current?.parts[index];
      if (index !== undefined && part !== undefined && part.type === "reasoning") {
        replacePart(builder, index, { ...part, text: part.text + delta });
      }
      return;
    }
    case "reasoning-end": {
      const { id } = TextEventSchema.parse(chunk);
      const index = builder.reasoningAt.get(id);
      const part = index === undefined ? undefined : builder.current?.parts[index];
      if (index !== undefined && part !== undefined && part.type === "reasoning") {
        replacePart(builder, index, { ...part, state: "done" });
      }
      return;
    }
    case "tool-input-start": {
      const { toolCallId, toolName } = ToolInputStartEventSchema.parse(chunk);
      builder.toolNameOf.set(toolCallId, toolName);
      const index = appendPart(builder, { type: `tool-${toolName}`, toolCallId, state: "input-streaming" });
      builder.toolAt.set(toolCallId, index);
      return;
    }
    case "tool-input-available": {
      const { toolCallId, toolName, input } = ToolInputAvailableEventSchema.parse(chunk);
      builder.toolNameOf.set(toolCallId, toolName);
      builder.toolInputOf.set(toolCallId, input);
      const part: Part = { type: `tool-${toolName}`, toolCallId, state: "input-available", input };
      const index = builder.toolAt.get(toolCallId);
      if (index === undefined) {
        const newIndex = appendPart(builder, part);
        builder.toolAt.set(toolCallId, newIndex);
      } else {
        replacePart(builder, index, part);
      }
      return;
    }
    case "tool-output-available": {
      const { toolCallId, output } = ToolOutputAvailableEventSchema.parse(chunk);
      const toolName = builder.toolNameOf.get(toolCallId) ?? "unknown";
      const input = builder.toolInputOf.get(toolCallId);
      const part: Part = { type: `tool-${toolName}`, toolCallId, state: "output-available", input, output };
      const index = builder.toolAt.get(toolCallId);
      if (index === undefined) {
        const newIndex = appendPart(builder, part);
        builder.toolAt.set(toolCallId, newIndex);
      } else {
        replacePart(builder, index, part);
      }
      return;
    }
    case "tool-output-error": {
      const { toolCallId, errorText } = ToolOutputErrorEventSchema.parse(chunk);
      const toolName = builder.toolNameOf.get(toolCallId) ?? "unknown";
      const input = builder.toolInputOf.get(toolCallId);
      const part: Part = { type: `tool-${toolName}`, toolCallId, state: "output-error", input, errorText };
      const index = builder.toolAt.get(toolCallId);
      if (index === undefined) {
        const newIndex = appendPart(builder, part);
        builder.toolAt.set(toolCallId, newIndex);
      } else {
        replacePart(builder, index, part);
      }
      return;
    }
    case "data-steer": {
      const { data } = DataEventSchema.parse(chunk);
      const steer = SteerDataSchema.parse(data);
      closeMessage(builder);
      builder.messages = [
        ...builder.messages,
        { id: freshId(builder, "steer"), role: "user", parts: [{ type: "text", text: steer.message }] },
      ];
      return;
    }
    default: {
      if (chunk.type.startsWith("data-")) {
        const { id, data } = DataEventSchema.parse(chunk);
        const suffix = chunk.type.slice("data-".length);
        appendPart(builder, { type: `data-${suffix}`, id, data });
      }
    }
  }
}

function stepStreamUrl(sessionId: string, node: string, item: string, since: number): string {
  const params = new URLSearchParams({ item, since: String(since) });
  return `${STUDIO_API_URL}/api/sessions/${encodeURIComponent(sessionId)}/run/steps/${encodeURIComponent(node)}/stream?${params.toString()}`;
}

export interface StepStreamState {
  messages: UIMessage[];
  streaming: boolean;
  // True once any connection for this line has reported its buffer already trimmed past what
  // the browser asked to resume from -- sticky for the line's life, a later reconnect that
  // finds nothing further dropped does not clear it back.
  droppedBefore: boolean;
}

const RECONNECT_BACKOFF_FLOOR_MS = 500;
const RECONNECT_BACKOFF_CEILING_MS = 5000;

// Waits out one backoff step, ending early (and promptly) when `signal` aborts -- an unmount
// must not leave a reconnect attempt pending for up to five seconds after the reader is gone.
function backoffDelay(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    function onAbort(): void {
      clearTimeout(timer);
      resolve();
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

// Tails one step's live conversation. Reconnects on a drop (the reader ends without `[DONE]`)
// at the number of chunks already folded in, so a resumed connection never replays content the
// browser already has. Every connection's own `data-replay-floor` frame names the absolute
// index its chunks start counting from -- read here rather than assumed, since a live buffer
// that has trimmed past what was asked for answers from further along than requested, and
// `droppedBefore` says so when it does. `enabled` false tears the connection down and answers
// the empty state -- the drawer passes it only while it needs a live tail (an open line of a
// live run).
export function useStepStream(sessionId: string, node: string, item: string, enabled: boolean): StepStreamState {
  const [state, setState] = useState<StepStreamState>({ messages: [], streaming: false, droppedBefore: false });

  useEffect(() => {
    if (!enabled) {
      setState({ messages: [], streaming: false, droppedBefore: false });
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    const builder = newBuilder();
    let chunksSeen = 0;
    let droppedBefore = false;

    setState({ messages: [], streaming: true, droppedBefore: false });

    async function readOnce(since: number): Promise<{ sawDone: boolean }> {
      const response = await fetch(stepStreamUrl(sessionId, node, item, since), {
        credentials: "include",
        signal: controller.signal,
      });
      if (!response.ok || response.body === null) {
        if (!cancelled) {
          setState((prev) => ({ ...prev, streaming: false }));
        }
        return { sawDone: true };
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      // Every connection opens with its own `data-replay-floor` frame -- checked once, on the
      // first data line this call sees, whether or not it parses.
      let floorChecked = false;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          return { sawDone: false };
        }
        buffer += decoder.decode(value, { stream: true });
        const { complete, rest } = dataLines(buffer);
        buffer = rest;
        let sawDone = false;
        for (const data of complete) {
          if (data === "[DONE]") {
            sawDone = true;
            continue;
          }
          const isFirstFrame = !floorChecked;
          floorChecked = true;
          try {
            const raw: unknown = JSON.parse(data);
            if (isFirstFrame) {
              const floorFrame = ReplayFloorChunkSchema.safeParse(raw);
              if (floorFrame.success) {
                chunksSeen = floorFrame.data.data.floor;
                if (floorFrame.data.data.floor > floorFrame.data.data.requested) {
                  droppedBefore = true;
                }
                continue;
              }
            }
            chunksSeen += 1;
            applyChunk(builder, raw);
          } catch {
            // A frame this client cannot parse (malformed JSON, or a recognized type whose
            // shape drifted) contributes nothing to the transcript, but its own absolute index
            // still counts, and the frames after it in this same batch still fold in normally.
            chunksSeen += 1;
          }
        }
        if (cancelled) {
          return { sawDone: true };
        }
        setState({ messages: snapshot(builder), streaming: !sawDone, droppedBefore });
        if (sawDone) {
          return { sawDone: true };
        }
      }
    }

    async function run(): Promise<void> {
      let backoffMs = RECONNECT_BACKOFF_FLOOR_MS;
      for (;;) {
        let result: { sawDone: boolean };
        try {
          result = await readOnce(chunksSeen);
        } catch {
          if (cancelled) {
            return;
          }
          await backoffDelay(backoffMs, controller.signal);
          backoffMs = Math.min(backoffMs * 2, RECONNECT_BACKOFF_CEILING_MS);
          if (cancelled) {
            return;
          }
          continue;
        }
        backoffMs = RECONNECT_BACKOFF_FLOOR_MS;
        if (cancelled || result.sawDone) {
          return;
        }
      }
    }

    void run();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [sessionId, node, item, enabled]);

  return state;
}

// ---- the archive converter: a line's stored ModelMessage payloads -> the same UIMessage shape ----

function userPromptText(payload: MessagePayload): string {
  return payload.parts
    .filter((part) => part.part_kind === "user-prompt" && typeof part.content === "string")
    .map((part) => String(part.content))
    .join("");
}

function toolReturnsByCallId(messages: MessagePayload[]): Map<string, unknown> {
  const returns = new Map<string, unknown>();
  for (const payload of messages) {
    if (payload.kind !== "request") {
      continue;
    }
    for (const part of payload.parts) {
      if (part.part_kind === "tool-return" && part.tool_call_id !== undefined) {
        returns.set(part.tool_call_id, part.content);
      }
    }
  }
  return returns;
}

function toolReturnTimestampsByCallId(messages: MessagePayload[]): Map<string, string> {
  const timestamps = new Map<string, string>();
  for (const payload of messages) {
    if (payload.kind !== "request") {
      continue;
    }
    for (const part of payload.parts) {
      if (part.part_kind === "tool-return" && part.tool_call_id !== undefined && typeof part.timestamp === "string") {
        timestamps.set(part.tool_call_id, part.timestamp);
      }
    }
  }
  return timestamps;
}

function responseToolCallIds(payload: MessagePayload): string[] {
  const ids: string[] = [];
  for (const part of payload.parts) {
    if (part.part_kind === "tool-call" && part.tool_call_id !== undefined) {
      ids.push(part.tool_call_id);
    }
  }
  return ids;
}

// Mirrors `responseDataStepPart` in `sessions.ts`: one response row's own usage and timestamp,
// `null` when the row carries neither.
function stepDataPart(payload: MessagePayload, toolReturnTimestamps: Map<string, string>): Part | null {
  if (payload.usage === undefined || typeof payload.timestamp !== "string") {
    return null;
  }
  const toolCallIds = responseToolCallIds(payload);
  const returns: Record<string, string> = {};
  for (const toolCallId of toolCallIds) {
    const timestamp = toolReturnTimestamps.get(toolCallId);
    if (timestamp !== undefined) {
      returns[toolCallId] = timestamp;
    }
  }
  return {
    type: "data-step",
    data: {
      tool_call_ids: toolCallIds,
      usage: payload.usage,
      timestamp: payload.timestamp,
      ...(Object.keys(returns).length === 0 ? {} : { returns }),
    },
  };
}

// One response row's own parts, in the order the model produced them: its thinking (kept, unlike
// the session transcript's own rebuild), its text, and EVERY tool call it made (not filtered to
// companion tools -- a step's tool calls are its content), each carrying its own return.
function stepResponseParts(payload: MessagePayload, toolReturns: Map<string, unknown>): Part[] {
  const parts: Part[] = [];
  for (const part of payload.parts) {
    if (part.part_kind === "text" && typeof part.content === "string") {
      parts.push({ type: "text", text: part.content });
    } else if (part.part_kind === "thinking" && typeof part.content === "string") {
      parts.push({ type: "reasoning", text: part.content });
    } else if (part.part_kind === "tool-call" && part.tool_name !== undefined && part.tool_call_id !== undefined) {
      const returned = toolReturns.get(part.tool_call_id);
      parts.push({
        type: `tool-${part.tool_name}`,
        toolCallId: part.tool_call_id,
        state: "output-available",
        input: part.args,
        output: returned === undefined ? NEVER_ANSWERED : returned,
      });
    }
  }
  return parts;
}

export interface StepConversationOptions {
  /** Where a call's own return is looked up, defaulting to `messages` itself. A turn cut into
   * segments for display passes the whole turn's messages here, so a call kept in one segment
   * still pairs with a return the cut carried into another rather than reading as pending. */
  toolReturnsFrom?: MessagePayload[];
}

// A step's stored conversation -> one user bubble per prompt (the briefing that opened it, and
// any later one a steer or a further turn added) and one assistant bubble per run of response
// rows between them, each response opening with its own `step-start` (and `data-step` when the
// row carries usage) the same way the session transcript's own bubbles do.
export function stepConversationToUiMessages(messages: MessagePayload[], options: StepConversationOptions = {}): UIMessage[] {
  const returnsFrom = options.toolReturnsFrom ?? messages;
  const toolReturns = toolReturnsByCallId(returnsFrom);
  const toolReturnTimestamps = toolReturnTimestampsByCallId(returnsFrom);
  const result: UIMessage[] = [];
  let assistantParts: Part[] = [];
  let assistantId = 0;

  function flushAssistant(): void {
    if (assistantParts.length > 0) {
      result.push({ id: `step-assistant-${assistantId}`, role: "assistant", parts: assistantParts });
    }
    assistantParts = [];
  }

  messages.forEach((payload, index) => {
    if (payload.kind === "request") {
      const text = userPromptText(payload);
      if (text !== "") {
        flushAssistant();
        result.push({ id: `step-user-${index}`, role: "user", parts: [{ type: "text", text }] });
      }
      return;
    }
    const parts = stepResponseParts(payload, toolReturns);
    if (parts.length === 0) {
      return;
    }
    if (assistantParts.length === 0) {
      assistantId = index;
    }
    const dataStep = stepDataPart(payload, toolReturnTimestamps);
    assistantParts.push({ type: "step-start" }, ...(dataStep === null ? [] : [dataStep]), ...parts);
  });
  flushAssistant();
  return result;
}
