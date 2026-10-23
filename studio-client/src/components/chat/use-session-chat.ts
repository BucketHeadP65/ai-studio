"use client";

import { useChat } from "@ai-sdk/react";
import { useQueryClient } from "@tanstack/react-query";
import { DefaultChatTransport, getToolName } from "ai";
import { useEffect, useMemo, useRef, useState } from "react";

import type { PageContext, ToolResult } from "@/api/page-context";
import { sessionsApi } from "@/api/sessions";
import { studioErrorMessage } from "@/api/studio";
import { STUDIO_API_URL } from "@/api/studio-config";
import { type ChatReply, ChatReplySchema } from "@/api/studio-types";
import { ApiError } from "@/shared/api/client";

import {
  type PendingApproval,
  hasActOutcome,
  lastStepToolParts,
  pendingApprovals,
  toolResultsOf,
  unresolvedVerbs,
} from "./tool-parts";

import type { UIMessage } from "ai";

// One chat engine per viewed session: every turn is posted to
// /api/sessions/{id}/messages, and the server holds the turn history, so the wire
// carries only the latest user message. The settled reply rides in on a data-settle
// part of the assistant message once the turn finishes streaming.
//
// A turn can also end deferred, its last step holding tool calls this browser owes an
// answer for: a verb runs here at once, a console act waits for the person. The engine
// answers what it can, holds what it must, and resumes the turn with the answers.

// What a verb outcome says when this surface has no executor wired to it.
const NO_EXECUTOR = "this surface cannot perform that";

// This stream's request body: pydantic-ai's own submit-message shape carrying the new user
// message alone (the server holds the history), beside the page context when a caller supplies
// one and the tool results the last turn left outstanding when there are any. `settled` names the
// calls this browser answered, so a transcript's own record of an earlier turn's calls never
// rides back as an answer. A continuation carries no message at all: its trailing message is the
// companion's own, and the answers beside it are what resume the turn.
//
// `designOnly` says this surface only designs, and the server then offers the turn no verb that
// starts a run or changes a standing schedule. It only ever narrows what the turn may reach, so a
// surface that says nothing gets the ordinary agent.
//
// `id` is the protocol's chat id, which the server reads as the conversation this turn belongs
// to, so it is the session's own id and not a fresh one per request.
export function buildChatBody(
  sessionId: string,
  messages: UIMessage[],
  context: PageContext | null,
  settled: ReadonlySet<string>,
  designOnly = false,
): {
  trigger: "submit-message";
  id: string;
  messages: UIMessage[];
  context?: PageContext;
  tool_results?: ToolResult[];
  design_only?: true;
} {
  const last = messages.at(-1);
  const toolResults = toolResultsOf(messages, settled);
  return {
    trigger: "submit-message",
    id: sessionId,
    messages: last?.role === "user" ? [last] : [],
    ...(context === null ? {} : { context }),
    ...(toolResults.length === 0 ? {} : { tool_results: toolResults }),
    ...(designOnly ? { design_only: true } : {}),
  };
}

// A turn with no data-settle part carries no settle, the same path a shape the schema
// rejects takes: this browser answers what it can and shows the turn without its card
// rather than losing the turn to a callback throwing.
//
// The parts are read from the end, so a turn that settles more than once (a turn waiting
// on this browser settles again when it resumes) hands over the settle it finished with.
function settleFromMessage(message: UIMessage): ChatReply | null {
  for (let index = message.parts.length - 1; index >= 0; index -= 1) {
    const part = message.parts[index];
    if (part?.type === "data-settle") {
      const parsed = ChatReplySchema.safeParse(part.data);
      if (!parsed.success) {
        console.error("a data-settle part did not match the reply contract", parsed.error);
        return null;
      }
      return parsed.data;
    }
  }
  return null;
}

// Which tool calls in the live stream's last step this browser may now report back.
// Empty means the turn stays where it is: the last step still holds a call waiting on
// this browser, or every answer in it has already ridden a request, or the answers are
// not this browser's own (a step the server settled itself, or a transcript read back
// from storage, where every call already reads as answered).
function callsToReport(
  messages: UIMessage[],
  settled: Set<string>,
  reported: Set<string>,
): string[] {
  const last = messages.at(-1);
  if (last?.role !== "assistant") return [];
  const parts = lastStepToolParts(last);
  if (parts.length === 0) return [];
  const waiting = parts.some(
    (part) =>
      part.state === "input-streaming" ||
      part.state === "input-available" ||
      part.state === "approval-requested",
  );
  if (waiting) return [];
  const ours = parts.map((part) => part.toolCallId).filter((toolCallId) => settled.has(toolCallId));
  if (ours.length === 0) return [];
  if (ours.every((toolCallId) => reported.has(toolCallId))) return [];
  return ours;
}

export interface SessionChat {
  messages: UIMessage[];
  status: "submitted" | "streaming" | "ready" | "error";
  // The console acts of the live turn's last step still waiting on the person.
  pendingApprovals: PendingApproval[];
  send: (text: string) => Promise<void>;
  // Hands text to the turn in flight through the steer endpoint, for a session answering
  // server-side that this tab is not streaming; a 409 sends it as an ordinary turn.
  steer: (text: string) => Promise<void>;
  // Answers one waiting act. The turn resumes once nothing in its step is left waiting.
  respond: (approvalId: string, approved: boolean, reason?: string) => Promise<void>;
  // Turns down every waiting act at once. The turn does not resume on its own.
  denyPending: (reason: string) => Promise<void>;
  stop: () => void;
  error: string | null;
}

export interface UseSessionChatOptions {
  sessionId: string;
  initialMessages: UIMessage[];
  onSettle: (reply: ChatReply) => void;
  // Read once per send, so a page's context (its ids, its tab) rides the wire as of
  // the moment the person hits send, not as of when the hook first mounted.
  getContext?: () => PageContext | null;
  // Says this surface only designs, so every turn it sends is offered no verb that starts a run
  // or changes a schedule. Narrows only: leaving it out is the ordinary agent.
  designOnly?: boolean;
  // Performs one client-executed verb and returns what to report back, or null when the
  // name is not a verb this surface handles. An outcome that only holds once the page has
  // caught up (a navigate) comes back as a promise, and the turn waits on it before it
  // resumes, so the context the continuation carries is the page the verb reached.
  executeTool?: (name: string, input: unknown) => string | Promise<string> | null;
}

export function useSessionChat(options: UseSessionChatOptions): SessionChat {
  const { sessionId, initialMessages, onSettle } = options;
  const [interruptError, setInterruptError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const executeRef = useRef(options.executeTool);
  executeRef.current = options.executeTool;

  // The tool calls this browser answered, and the ones whose answers have already ridden
  // a request. Together they keep a verb from running twice and a step from resuming
  // twice, across any number of re-renders.
  const settled = useRef(new Set<string>());
  const reported = useRef(new Set<string>());
  // Set while a turn this browser stopped is being torn down, so nothing left half
  // announced in it is performed or reported.
  const halted = useRef(false);

  // The bookkeeping above names calls of one session's history. Another session's ids
  // mean nothing here, so switching sessions starts it over.
  const scope = useRef(sessionId);
  if (scope.current !== sessionId) {
    scope.current = sessionId;
    settled.current = new Set();
    reported.current = new Set();
    halted.current = false;
  }

  const transport = useMemo(
    () =>
      new DefaultChatTransport({
        api: `${STUDIO_API_URL}/api/sessions/${encodeURIComponent(sessionId)}/messages`,
        credentials: "include",
        prepareSendMessagesRequest: ({ messages }) => ({
          body: buildChatBody(
            sessionId,
            messages,
            options.getContext?.() ?? null,
            settled.current,
            options.designOnly ?? false,
          ),
        }),
      }),
    [sessionId, options.getContext, options.designOnly],
  );

  const chat = useChat({
    id: sessionId,
    messages: initialMessages,
    transport,
    onFinish: ({ message }) => {
      const reply = settleFromMessage(message);
      if (reply !== null) {
        onSettle(reply);
      }
      // An act that reached an outcome changed something the dashboard is showing.
      if (hasActOutcome(message)) {
        void queryClient.invalidateQueries();
      }
    },
  });

  const pending = useMemo(() => pendingApprovals(chat.messages), [chat.messages]);

  // A stream that ended deferred leaves work here: perform each verb once, then, once
  // nothing in the step is still waiting, resume the turn with what this browser
  // answered. Both halves run off the live stream state, one effect pass apiece, so the
  // continuation always sees the outputs the verbs produced.
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed on the SDK's own status and message snapshots, not on the chat client, which is a new object every render
  useEffect(() => {
    if (halted.current) return;
    if (chat.status !== "ready") return;

    const verbs = unresolvedVerbs(chat.messages).filter(
      (part) => !settled.current.has(part.toolCallId),
    );
    if (verbs.length > 0) {
      for (const part of verbs) {
        settled.current.add(part.toolCallId);
      }
      void (async () => {
        for (const part of verbs) {
          const name = getToolName(part);
          const output = await (executeRef.current?.(name, part.input) ?? NO_EXECUTOR);
          await chat.addToolOutput({ tool: name, toolCallId: part.toolCallId, output });
        }
      })();
      return;
    }

    const toReport = callsToReport(chat.messages, settled.current, reported.current);
    if (toReport.length === 0) return;
    for (const toolCallId of toReport) {
      reported.current.add(toolCallId);
    }
    void chat.sendMessage();
  }, [chat.status, chat.messages]);

  // The approval id is recorded beside the call it answers, so an answer arriving from a
  // render that no longer lists it still counts as this browser's own.
  async function respond(approvalId: string, approved: boolean, reason?: string): Promise<void> {
    settled.current.add(approvalId);
    const match = pending.find((approval) => approval.approvalId === approvalId);
    if (match !== undefined) {
      settled.current.add(match.toolCallId);
    }
    await chat.addToolApprovalResponse({ id: approvalId, approved, reason });
  }

  // Every answer this browser has already given in the live step rides the request the
  // caller is about to send, so recording them as reported before it goes keeps the turn
  // from resuming beside that request.
  function markStepReported(): void {
    const last = chat.messages.at(-1);
    if (last?.role !== "assistant") return;
    for (const part of lastStepToolParts(last)) {
      if (settled.current.has(part.toolCallId)) {
        reported.current.add(part.toolCallId);
      }
    }
  }

  // Marking each denial as already reported keeps the turn from resuming on its own:
  // the caller decides what rides next, and the denials travel with it. Answers already
  // given in this step are swept for the same reason, so handing the composer back parks
  // the turn rather than starting one nobody asked for. Nothing is lost by it: the
  // outcomes ride whichever request goes next, read off the messages themselves.
  async function denyPending(reason: string): Promise<void> {
    markStepReported();
    for (const approval of pending) {
      settled.current.add(approval.toolCallId);
      reported.current.add(approval.toolCallId);
      await chat.addToolApprovalResponse({ id: approval.approvalId, approved: false, reason });
    }
  }

  // The ordinary turn of one's own: close out what the last step still owes, then stream.
  async function sendTurn(text: string): Promise<void> {
    markStepReported();
    await denyPending("superseded");
    await chat.sendMessage({ text });
  }

  // Hands `text` to the turn already in flight through the steer endpoint. A 409 means the turn
  // settled in the race (or the answering flag outlived it), so the text goes as its own turn.
  // Callable directly for a session answering server-side that this tab is NOT streaming (a
  // reload mid-turn): there is no local stream to key off, only the server's own word.
  async function steer(text: string): Promise<void> {
    setInterruptError(null);
    halted.current = false;
    try {
      await sessionsApi.steer(sessionId, text);
      chat.setMessages([
        ...chat.messages,
        { id: `steer-${crypto.randomUUID()}`, role: "user", parts: [{ type: "text", text }] },
      ]);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await sendTurn(text);
        return;
      }
      setInterruptError(studioErrorMessage(error));
    }
  }

  async function send(text: string): Promise<void> {
    setInterruptError(null);
    halted.current = false;
    // Waiting approvals are superseded by the person typing; that is an ordinary turn's job.
    if (pending.length > 0) {
      await sendTurn(text);
      return;
    }
    // Every other send knocks on the steer endpoint first and lets the SERVER decide: a turn in
    // flight anywhere (this tab's stream, another tab, a session this tab's answering flag has
    // not caught up with) takes the message mid-turn, and the endpoint's 409 means idle, so it
    // goes out as an ordinary turn. Guessing from local status is what used to leak a raw
    // "a turn is already streaming" 409 into the notice area when the guess lagged.
    await steer(text);
  }

  // The SDK's own `stop()` only aborts this tab's fetch of the stream; the turn
  // keeps running on the server until the session itself is told to stop.
  function stop(): void {
    halted.current = true;
    chat.stop();
    sessionsApi.interrupt(sessionId).catch((error: unknown) => {
      setInterruptError(studioErrorMessage(error));
    });
  }

  return {
    messages: chat.messages,
    status: chat.status,
    pendingApprovals: pending,
    send,
    steer,
    respond,
    denyPending,
    stop,
    error: interruptError ?? chat.error?.message ?? null,
  };
}
