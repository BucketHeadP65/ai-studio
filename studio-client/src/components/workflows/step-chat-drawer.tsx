"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Drawer } from "client-core/ui/drawer";
import { isTextUIPart } from "ai";
import { type ReactElement, useMemo, useState } from "react";

import { stepKeys } from "@/api/query-keys";
import type { MessagePayload } from "@/api/sessions-types";
import {
  StepNotSteerableError,
  stepChatApi,
  stepConversationToUiMessages,
  useStepStream,
} from "@/api/step-chat";
import type { GraphNode } from "@/api/studio-types";
import { AssistantTurn } from "@/components/chat/assistant-turn";
import { Composer, useComposerDraft } from "@/components/chat/composer";
import { ThinkingMark } from "@/components/chat/thinking-mark";
import { AgentMarkdown } from "@/components/conversation/markdown";

import type { UIMessage } from "ai";

// One agentic step's own conversation: its brief, its thinking, every tool act, a live tail
// while the underlying line is still executing, and a composer that steers it. Opened from a
// node click on the run canvas -- a live run passes this session's own id, a selected archived
// run passes its stored `conversations` instead, exactly one of the two ever set.
//
// A node that ran more than once (a map's per-item lines) shows a picker first; picking a line
// lands on that line's own conversation. `AssistantTurn`'s own fold drops reasoning parts (it
// was built for the orchestrator's turns, which never carry any), so this drawer renders a
// step's thinking itself, ahead of the reused turn bubble, rather than losing it.

export interface StepChatDrawerProps {
  sessionId: string | null;
  recordConversations: Record<string, MessagePayload[]> | null;
  node: GraphNode;
  open: boolean;
  onClose: () => void;
}

interface DrawerLine {
  item: string;
  executing: boolean;
  settled: boolean;
}

const FINISHED_NOTICE = "This step has finished. Its conversation is read only.";
const NOT_STARTED_NOTICE = "This step has not started yet.";
const COULD_NOT_READ_NOTICE = "Could not read this step.";
const DROPPED_NOTICE =
  "Earlier output was dropped from the live buffer. The full conversation is archived when the run ends.";

// The archive's own conversations dict is keyed `"{node id}[{item}]"` ALWAYS (`archive.py`'s
// `_jsonable_conversations` writes it unconditionally, never a bare id), by the graph node id,
// never `work` -- a one-shot run step is still `"draft[]"`, an empty item, not a bare `"draft"`.
function archivedKeyOf(nodeId: string, item: string): string {
  return `${nodeId}[${item}]`;
}

// Every line this node ran, read off that key shape: one entry per `"{nodeId}[{item}]"` key,
// `item` being whatever sits between the brackets (empty for the one line a plain run step
// carries). An archived line is always settled and never executing -- the record is closed.
function archivedLinesOf(conversations: Record<string, MessagePayload[]>, nodeId: string): DrawerLine[] {
  const marker = `${nodeId}[`;
  const lines: DrawerLine[] = [];
  for (const key of Object.keys(conversations)) {
    if (key.startsWith(marker) && key.endsWith("]")) {
      lines.push({ item: key.slice(marker.length, -1), executing: false, settled: true });
    }
  }
  return lines;
}

function textOf(message: UIMessage): string {
  return message.parts
    .filter(isTextUIPart)
    .map((part) => part.text)
    .join("");
}

type ReasoningPart = Extract<UIMessage["parts"][number], { type: "reasoning" }>;

function isReasoningPart(part: UIMessage["parts"][number]): part is ReasoningPart {
  return part.type === "reasoning";
}

function ReasoningNote({ message }: { message: UIMessage }): ReactElement | null {
  const reasoning = message.parts.filter(isReasoningPart);
  if (reasoning.length === 0) return null;
  return (
    <div className="flex w-full flex-col gap-1 rounded-md border border-line bg-well px-3 py-2">
      <p className="text-label-3 text-micro font-semibold uppercase tracking-caps">Thinking</p>
      {reasoning.map((part, index) => (
        <AgentMarkdown key={`${message.id}-reasoning-${index}`} text={part.text} />
      ))}
    </div>
  );
}

function UserBubble({ text }: { text: string }): ReactElement {
  return (
    <div className="flex max-w-[92%] flex-col self-end">
      <p className="mb-1 text-right text-label-3 text-micro font-semibold uppercase tracking-caps">
        You
      </p>
      <div className="max-w-[480px] break-words rounded-tl-lg rounded-tr-lg rounded-bl-lg rounded-br-sm bg-label-1 px-4 py-3.5 text-body text-surface">
        {text}
      </div>
    </div>
  );
}

// The step's own speaker label, matching the drawer title (`Step <node>`) with the item folded
// in for a map line, so the bubble reads as whichever run this conversation actually is.
function speakerFor(nodeId: string, item: string): string {
  return item === "" ? `Step ${nodeId}` : `Step ${nodeId} (${item})`;
}

function StepConversation({
  messages,
  streaming,
  speaker,
}: {
  messages: UIMessage[];
  streaming: boolean;
  speaker: string;
}): ReactElement {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 py-4">
      {messages.map((message, index) =>
        message.role === "user" ? (
          <UserBubble key={message.id} text={textOf(message)} />
        ) : (
          <div key={message.id} className="flex max-w-[92%] flex-col items-start gap-2 self-start">
            <ReasoningNote message={message} />
            <AssistantTurn message={message} speaker={speaker} live={streaming && index === messages.length - 1} />
          </div>
        ),
      )}
      {streaming && messages.length === 0 ? (
        <div className="flex items-start gap-2.5 self-start">
          <ThinkingMark startedAt={null} tokensOut={null} />
        </div>
      ) : null}
      {!streaming && messages.length === 0 ? (
        <p className="text-body text-label-3">No conversation recorded for this step.</p>
      ) : null}
    </div>
  );
}

function ItemPicker({
  lines,
  onSelect,
}: {
  lines: DrawerLine[];
  onSelect: (item: string) => void;
}): ReactElement {
  return (
    <div className="flex flex-col gap-3 px-4 py-4">
      <p className="text-body text-label-3">
        This step ran more than once. Pick one to view its conversation.
      </p>
      <div className="flex flex-wrap gap-2">
        {lines.map((line) => (
          <button
            key={line.item}
            type="button"
            onClick={() => onSelect(line.item)}
            className="rounded-md border border-line bg-surface px-3 py-1.5 text-caption text-label-1 transition-colors hover:bg-well"
          >
            {line.item}
            {line.executing ? " (running)" : ""}
          </button>
        ))}
      </div>
    </div>
  );
}

export function StepChatDrawer({
  sessionId,
  recordConversations,
  node,
  open,
  onClose,
}: StepChatDrawerProps): ReactElement {
  const [selectedItem, setSelectedItem] = useState<string | null>(null);
  const [steerError, setSteerError] = useState<string | null>(null);
  const draft = useComposerDraft();

  // `stepKeys.lines` sits under the "run" root (`query-keys.ts`), so every push frame a run
  // publishes -- `run.{workflow_id}` on each node start and finish -- invalidates and refetches
  // this. That alone still leaves a gap the moment a node's own "started" frame lands: its line
  // is not open yet, so an event-only refetch can read the listing a beat too early. The 2s poll
  // is the floor that closes it.
  const linesQuery = useQuery({
    queryKey: stepKeys.lines(sessionId ?? ""),
    queryFn: () => stepChatApi.lines(sessionId ?? ""),
    enabled: sessionId !== null && open,
    refetchInterval: sessionId !== null ? 2000 : false,
  });

  const lines: DrawerLine[] =
    sessionId !== null
      ? (linesQuery.data ?? [])
          .filter((line) => line.node === node.id)
          .map((line) => ({ item: line.item, executing: line.executing, settled: line.settled }))
      : recordConversations !== null
        ? archivedLinesOf(recordConversations, node.id)
        : [];

  const effectiveItem = selectedItem ?? (lines.length === 1 ? (lines[0]?.item ?? null) : null);
  const activeLine = effectiveItem === null ? null : (lines.find((line) => line.item === effectiveItem) ?? null);

  const streamEnabled = sessionId !== null && open && effectiveItem !== null;
  const liveStream = useStepStream(sessionId ?? "", node.id, effectiveItem ?? "", streamEnabled);

  const steer = useMutation({
    mutationFn: (text: string) => stepChatApi.steer(sessionId ?? "", node.id, effectiveItem ?? "", text),
    onError: (error: unknown) => {
      setSteerError(error instanceof StepNotSteerableError ? error.message : "Could not send that message.");
    },
    onSuccess: () => setSteerError(null),
  });

  const archivedMessages = useMemo(
    () =>
      recordConversations !== null && effectiveItem !== null
        ? stepConversationToUiMessages(recordConversations[archivedKeyOf(node.id, effectiveItem)] ?? [])
        : [],
    [recordConversations, node.id, effectiveItem],
  );
  const messages: UIMessage[] = sessionId !== null ? liveStream.messages : archivedMessages;
  const streaming = sessionId !== null ? liveStream.streaming : false;
  const speaker = speakerFor(node.id, effectiveItem ?? "");

  // The composer's own state comes from the line's `executing` flag, never from the stream's
  // `streaming` bit -- that bit cannot tell a fetch failure from a settled line. Three notices,
  // live only (an archived run always finds its line in `recordConversations` and reads as
  // finished, unchanged): a failed lines fetch reads as unreadable rather than finished; no line
  // yet for this node or item reads as not started; only a line that answered and stopped reads
  // as finished.
  const executing = activeLine?.executing ?? false;
  const notice =
    sessionId !== null && linesQuery.isError
      ? COULD_NOT_READ_NOTICE
      : sessionId !== null && activeLine === null
        ? NOT_STARTED_NOTICE
        : executing
          ? steerError
          : FINISHED_NOTICE;
  const loadingLines = sessionId !== null && linesQuery.isPending;
  const droppedBefore = sessionId !== null && liveStream.droppedBefore;

  return (
    <Drawer open={open} title={`Step ${node.id}`} onClose={onClose}>
      <div className="flex h-full flex-col">
        {loadingLines ? (
          <p className="px-4 py-4 text-body text-label-3">Loading this step.</p>
        ) : effectiveItem === null && lines.length > 1 ? (
          <ItemPicker lines={lines} onSelect={setSelectedItem} />
        ) : (
          <>
            {lines.length > 1 ? (
              <div className="border-line border-b px-4 py-2">
                <button
                  type="button"
                  onClick={() => setSelectedItem(null)}
                  className="text-caption text-label-3 underline-offset-2 hover:text-label-1 hover:underline"
                >
                  Choose another item
                </button>
              </div>
            ) : null}
            {droppedBefore ? (
              <p className="px-4 pt-3 text-caption text-label-3">{DROPPED_NOTICE}</p>
            ) : null}
            <StepConversation messages={messages} streaming={streaming} speaker={speaker} />
            <Composer
              onSend={(text) => {
                setSteerError(null);
                steer.mutate(text);
              }}
              busy={!executing}
              onStop={null}
              notice={notice}
              draft={draft}
            />
          </>
        )}
      </div>
    </Drawer>
  );
}
