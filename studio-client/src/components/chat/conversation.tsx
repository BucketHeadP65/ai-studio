"use client";

import { type UIMessage, getToolName, isDynamicToolUIPart, isTextUIPart, isToolUIPart } from "ai";
import { cn } from "client-core/lib/cn";
import { Fragment, type ReactElement } from "react";

import type { RunView, TranscriptMessage } from "@/api/sessions-types";
import type {
  ChatReply,
  GateMode,
  PendingGate,
  StepEvent,
  WorkflowGraph,
} from "@/api/studio-types";
import {
  ConversationContent,
  ConversationScrollButton,
  Conversation as StickConversation,
} from "@/components/ai-elements/conversation";
import { NodeGlyph } from "@/components/marks/node-glyph";
import { useNowTick } from "@/components/workflows/elapsed";
import { OpenRunLink } from "@/components/workflows/open-run-link";
import { RUN_TOOL_NAME } from "./run-tool-calls";

import { AssistantTurn } from "./assistant-turn";
import { DraftCard } from "./draft-card";
import { GateTurn } from "./gate-turn";
import { runToolCallEntries } from "./run-tool-calls";
import { RunTurn } from "./run-turn";
import { ThinkingMark } from "./thinking-mark";

// The center column: the running dialogue rendered straight from the wire's own UIMessage
// stream. A user turn renders as plain text. An assistant turn renders as one bubble whose
// body is its last step, its earlier steps folded above it (`AssistantTurn`), with the turn
// inspector a click away. The draft card lands
// under whichever assistant message is the most recent one, the moment the reply carries a
// graph. An info-box turn lands right after whichever bubble closes out the same model turn a
// run-tool call happened in, read off the stored transcript (`runToolCallEntries`), never the
// streaming wire. This session's own live run, when
// it has one, always renders as its own turn after the last message; the gate turn, when that
// run is waiting, closes the transcript right after it.
//
// Scroll behavior comes from the vendored `StickConversation`/`ConversationContent`: the reader
// stays anchored to the bottom only while already there, and a scroll button appears once they
// have moved away from it, rather than a scroll-into-view effect dragging them back down.

export interface LiveGate {
  pending: PendingGate;
  startedAt: string;
  events: StepEvent[];
}

export interface LiveRun {
  title: string;
  run: RunView;
  graph: WorkflowGraph | undefined;
}

function textOf(message: UIMessage): string {
  return message.parts
    .filter(isTextUIPart)
    .map((part) => part.text)
    .join("");
}

// Whether this message carries any tool call the bubble draws -- a verb, a console act, or
// a work tool, whatever state it is in. A bubble with nothing else to show still renders for
// these: the act line or the work card is its answer. Only the run tool stays out, since its
// run card stands in for the whole call.
function hasActParts(message: UIMessage): boolean {
  return message.parts
    .filter((part) => isToolUIPart(part) || isDynamicToolUIPart(part))
    .some((part) => getToolName(part) !== RUN_TOOL_NAME);
}

function RunStartedTurn({ workflowId }: { workflowId: string }): ReactElement {
  return (
    <div className="flex max-w-[92%] items-start gap-2.5 self-start">
      <NodeGlyph kind="agent-gate" fill="ran" size={30} className="invisible" />
      <div className="flex min-w-0 flex-1 items-start gap-3 rounded-lg border border-line bg-well p-3.5">
        <NodeGlyph kind="agent-gate" fill="ran" size={22} />
        <div className="flex min-w-0 flex-col items-start gap-1.5">
          <p className="text-caption text-label-3">
            The agent started a run of <span className="font-mono">{workflowId}</span>.
          </p>
          <OpenRunLink href={`/workflows/${encodeURIComponent(workflowId)}`} />
        </div>
      </div>
    </div>
  );
}

export function Conversation(props: {
  messages: UIMessage[];
  transcript: TranscriptMessage[];
  streaming: boolean;
  reply: ChatReply | null;
  sessionId: string | null;
  gateMode: GateMode;
  hasLiveRun: boolean;
  onChangeGates: () => void;
  liveRun: LiveRun | null;
  liveGate: LiveGate | null;
  onDecide: (approve: boolean, feedback?: string) => void;
  deciding: boolean;
  onInspect: (message: UIMessage) => void;
  /** When the live turn started, ISO; `null` shows the thinking mark's verb alone. */
  turnStartedAt?: string | null;
  /** A settled turn starts with its earlier steps folded; off, every step stays in view. */
  foldSettled?: boolean;
}): ReactElement {
  const {
    messages,
    transcript,
    streaming,
    reply,
    sessionId,
    gateMode,
    hasLiveRun,
    onChangeGates,
    liveRun,
    liveGate,
    onDecide,
    deciding,
    onInspect,
    turnStartedAt = null,
    foldSettled = false,
  } = props;
  const now = useNowTick(1000, liveRun !== null);

  let lastAssistantIndex = -1;
  messages.forEach((message, index) => {
    if (message.role === "assistant") lastAssistantIndex = index;
  });

  const entriesByMessageId = new Map<string, string[]>();
  for (const entry of runToolCallEntries(messages, transcript)) {
    const list = entriesByMessageId.get(entry.afterMessageId) ?? [];
    list.push(entry.workflowId);
    entriesByMessageId.set(entry.afterMessageId, list);
  }

  return (
    <StickConversation className="min-h-0" aria-live="off">
      <ConversationContent
        className="mx-auto w-full max-w-[1400px] gap-4 px-5 py-4"
        scrollClassName="overflow-x-hidden overflow-y-auto"
      >
        {messages.map((message, index) => {
          const isUser = message.role === "user";
          const startedWorkflowIds = entriesByMessageId.get(message.id) ?? [];
          // A turn whose whole answer was starting a run has no prose to show: the run's own card
          // (below, or the live run turn) is that answer, so the empty bubble stays out. A turn
          // that also carries a verb, a console act, or a work tool keeps its bubble regardless --
          // the act line or the work card is what that turn has to show.
          const silentRunStart =
            !isUser &&
            startedWorkflowIds.length > 0 &&
            textOf(message).trim() === "" &&
            !hasActParts(message);
          if (silentRunStart) {
            return (
              <Fragment key={message.id}>
                {startedWorkflowIds.map((workflowId) => (
                  <RunStartedTurn key={`${message.id}:${workflowId}`} workflowId={workflowId} />
                ))}
              </Fragment>
            );
          }
          return (
            <Fragment key={message.id}>
              <div className={cn("flex max-w-[92%] flex-col", isUser ? "self-end" : "self-start")}>
                {isUser ? (
                  <>
                    <p className="mb-1 text-right text-label-3 text-micro font-semibold uppercase tracking-caps">
                      You
                    </p>
                    <div className="max-w-[640px] break-words rounded-tl-lg rounded-tr-lg rounded-bl-lg rounded-br-sm bg-label-1 px-4 py-3.5 text-body text-surface">
                      {textOf(message)}
                    </div>
                  </>
                ) : (
                  <AssistantTurn
                    message={message}
                    onInspect={onInspect}
                    live={streaming && index === messages.length - 1}
                    turnStartedAt={turnStartedAt}
                    foldSettled={foldSettled}
                  />
                )}
                {!isUser && reply?.graph != null && index === lastAssistantIndex ? (
                  <DraftCard
                    reply={reply}
                    sessionId={sessionId}
                    gateMode={gateMode}
                    hasLiveRun={hasLiveRun}
                    onChangeGates={onChangeGates}
                  />
                ) : null}
              </div>
              {startedWorkflowIds.map((workflowId) => (
                <RunStartedTurn key={`${message.id}:${workflowId}`} workflowId={workflowId} />
              ))}
            </Fragment>
          );
        })}
        {liveRun !== null ? (
          <RunTurn title={liveRun.title} run={liveRun.run} graph={liveRun.graph} now={now} />
        ) : null}
        {liveGate !== null ? (
          <GateTurn
            pending={liveGate.pending}
            startedAt={liveGate.startedAt}
            events={liveGate.events}
            onDecide={onDecide}
            deciding={deciding}
          />
        ) : streaming ? (
          messages.at(-1)?.role !== "assistant" ? (
            // The turn's bubble before its first part arrives: the mark alone, in the agent's
            // own bubble, so nothing else ever stands in for the turn.
            <div className="flex max-w-[92%] items-start gap-2.5 self-start">
              <NodeGlyph kind="agent-gate" fill="ran" size={30} />
              <div className="flex min-w-0 flex-1 flex-col items-start gap-1 rounded-tl-sm rounded-tr-lg rounded-br-lg rounded-bl-lg border border-line bg-surface px-3.5 py-3 text-left shadow-sm">
                <p className="text-label-3 text-micro font-semibold uppercase tracking-caps">
                  Orchestrator agent
                </p>
                <ThinkingMark startedAt={turnStartedAt} tokensOut={null} />
              </div>
            </div>
          ) : null
        ) : null}
      </ConversationContent>
      <ConversationScrollButton
        aria-label="Scroll to bottom"
        className="rounded-full border-line bg-surface text-label-2 shadow-md"
      />
    </StickConversation>
  );
}
