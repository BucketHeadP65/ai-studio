"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  type UIMessage,
  getToolName,
  isDynamicToolUIPart,
  isReasoningUIPart,
  isTextUIPart,
  isToolUIPart,
} from "ai";
import { Drawer } from "client-core/ui/drawer";
import { Wrench } from "lucide-react";
import { type ReactElement, useState } from "react";

import { sessionKeys } from "@/api/query-keys";
import { parseTranscriptPayload, transcriptTurns } from "@/api/sessions";
import type { TranscriptMessage } from "@/api/sessions-types";
import { AgentMarkdown } from "@/components/conversation/markdown";

import { JsonTree, LONG_STRING_THRESHOLD } from "./json-tree";
import { readableToolText } from "./reply-text";
import { TextViewer } from "./text-viewer";

// Logfire-style, for one assistant reply: everything from the person's words that opened
// the turn through that reply, as a list of rows -- one per pydantic-ai message part, across
// every run the turn took. A row's source is either a stored transcript slice (the message id
// names a run and an idx the cached transcript actually has) or, for a message still only
// live on the wire, the UIMessage's own parts.

type Row =
  | { key: string; kind: "user-prompt"; text: string }
  | { key: string; kind: "text"; text: string }
  | { key: string; kind: "thinking"; text: string }
  | { key: string; kind: "tool-call"; toolName: string; args: unknown }
  | { key: string; kind: "tool-return"; toolName: string; content: unknown }
  | { key: string; kind: "retry-prompt"; text: string; toolName: string | null }
  | { key: string; kind: "other"; label: string; value: unknown };

function assertNever(value: never): never {
  throw new Error(`unhandled row kind ${JSON.stringify(value)}`);
}

function textOfContent(content: unknown): string {
  return typeof content === "string" ? content : JSON.stringify(content);
}

function tryParseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function rowsFromStoredTurn(messages: TranscriptMessage[], runId: string, idx: number): Row[] {
  const isSelected = (row: TranscriptMessage): boolean => row.run_id === runId && row.idx === idx;
  const turn = transcriptTurns(messages).find((candidate) => candidate.rows.some(isSelected));
  if (turn === undefined) {
    return [];
  }
  const selectedPosition = turn.rows.findIndex(isSelected);

  const rows: Row[] = [];
  for (const message of turn.rows.slice(0, selectedPosition + 1)) {
    const payload = parseTranscriptPayload(message.payload);
    if (payload === null) {
      continue;
    }
    payload.parts.forEach((part, partIndex) => {
      const key = `${message.run_id}:${message.idx}:${partIndex}`;
      switch (part.part_kind) {
        case "user-prompt":
          rows.push({ key, kind: "user-prompt", text: textOfContent(part.content) });
          break;
        case "text":
          rows.push({ key, kind: "text", text: textOfContent(part.content) });
          break;
        case "thinking":
          rows.push({ key, kind: "thinking", text: textOfContent(part.content) });
          break;
        case "tool-call":
          rows.push({
            key,
            kind: "tool-call",
            toolName: part.tool_name ?? "tool",
            args: part.args ?? null,
          });
          break;
        case "tool-return":
          rows.push({
            key,
            kind: "tool-return",
            toolName: part.tool_name ?? "tool",
            content: part.content ?? null,
          });
          break;
        case "retry-prompt":
          rows.push({
            key,
            kind: "retry-prompt",
            text: textOfContent(part.content),
            toolName: part.tool_name ?? null,
          });
          break;
        default:
          rows.push({ key, kind: "other", label: part.part_kind, value: part });
      }
    });
  }
  return rows;
}

function rowsFromLiveMessage(message: UIMessage): Row[] {
  const rows: Row[] = [];
  message.parts.forEach((part, partIndex) => {
    const key = `${message.id}:${partIndex}`;
    if (isTextUIPart(part)) {
      rows.push({ key, kind: message.role === "user" ? "user-prompt" : "text", text: part.text });
    } else if (isReasoningUIPart(part)) {
      rows.push({ key, kind: "thinking", text: part.text });
    } else if (isToolUIPart(part) || isDynamicToolUIPart(part)) {
      const toolName = getToolName(part);
      if (part.input !== undefined) {
        rows.push({ key: `${key}:call`, kind: "tool-call", toolName, args: part.input });
      }
      if (part.state === "output-available") {
        rows.push({ key: `${key}:return`, kind: "tool-return", toolName, content: part.output });
      } else if (part.state === "output-error") {
        rows.push({ key: `${key}:return`, kind: "tool-return", toolName, content: part.errorText });
      }
    }
  });
  return rows;
}

// A message id is `${run_id}:${idx}`; the run id itself never contains a colon
// (it's a UUID), so the last colon in the id always separates the two.
function parseMessageId(id: string): { runId: string; idx: number } | null {
  const lastColon = id.lastIndexOf(":");
  if (lastColon === -1) {
    return null;
  }
  const idx = Number(id.slice(lastColon + 1));
  if (!Number.isFinite(idx)) {
    return null;
  }
  return { runId: id.slice(0, lastColon), idx };
}

export interface TurnInspectorProps {
  sessionId: string;
  message: UIMessage;
  onClose: () => void;
}

export function TurnInspector({ sessionId, message, onClose }: TurnInspectorProps): ReactElement {
  const queryClient = useQueryClient();
  const [viewer, setViewer] = useState<{
    title: string;
    text: string;
    mode: "markdown" | "plain";
  } | null>(null);

  const cached = queryClient.getQueryData<TranscriptMessage[]>(sessionKeys.transcript(sessionId));
  const parsedId = parseMessageId(message.id);
  const storedMatch =
    cached !== undefined && parsedId !== null
      ? cached.some((entry) => entry.run_id === parsedId.runId && entry.idx === parsedId.idx)
      : false;

  const rows =
    storedMatch && cached !== undefined && parsedId !== null
      ? rowsFromStoredTurn(cached, parsedId.runId, parsedId.idx)
      : rowsFromLiveMessage(message);

  // "markdown" is the default: only the model's own prose reads well folded through the
  // markdown pipeline. Every system-generated payload (a tool call's arguments, a tool
  // return, an unrendered part kind) is passed "plain" at its own call site below, so it
  // opens as the verbatim, monospaced view instead of being folded into one paragraph.
  function openViewer(title: string, text: string, mode: "markdown" | "plain" = "markdown") {
    setViewer({ title, text, mode });
  }

  // The drawer is non-modal so the session rail and the conversation stay live beside it.
  // The viewer renders inside the drawer's own tree: a non-modal layer dismisses when focus
  // or a pointer lands outside it, and a nested surface counts as inside only from within.
  return (
    <Drawer
      open
      modal={false}
      title="Turn detail"
      onClose={onClose}
      className="w-[min(92vw,620px)]"
    >
      <div className="flex h-full flex-col gap-2 overflow-y-auto px-3 py-3">
        {rows.length === 0 ? (
          <p className="text-label-3 text-caption">Nothing recorded for this turn.</p>
        ) : (
          rows.map((row) => <InspectorRow key={row.key} row={row} onView={openViewer} />)
        )}
      </div>
      <TextViewer
        open={viewer !== null}
        title={viewer?.title ?? "Value"}
        text={viewer?.text ?? ""}
        mode={viewer?.mode ?? "plain"}
        onClose={() => setViewer(null)}
      />
    </Drawer>
  );
}

function ToolReturnText({
  content,
  toolName,
  onView,
}: {
  content: string;
  toolName: string;
  onView: (title: string, text: string, mode?: "markdown" | "plain") => void;
}): ReactElement {
  if (content.length <= LONG_STRING_THRESHOLD) {
    return <span className="whitespace-pre-wrap font-mono text-label-2">{content}</span>;
  }
  return (
    <div className="flex flex-wrap items-baseline gap-1.5">
      <span className="font-mono text-label-2">{content.slice(0, LONG_STRING_THRESHOLD)}</span>
      <button
        type="button"
        onClick={() => onView(`${toolName} return`, readableToolText(content), "plain")}
        className="text-accent text-caption underline"
      >
        View
      </button>
    </div>
  );
}

function ToolBadge({ toolName, act }: { toolName: string; act: string }): ReactElement {
  return (
    <div className="flex items-center gap-1.5">
      <Wrench size={12} strokeWidth={1.75} className="text-label-3" aria-hidden />
      <span className="rounded-full bg-accent-soft px-1.5 py-0.5 font-mono text-accent">
        {toolName}
      </span>
      <span className="text-label-3">{act}</span>
    </div>
  );
}

function InspectorRow({
  row,
  onView,
}: {
  row: Row;
  onView: (title: string, text: string, mode?: "markdown" | "plain") => void;
}): ReactElement {
  switch (row.kind) {
    case "user-prompt":
      return (
        <div className="rounded-md bg-accent-soft px-2.5 py-2 text-label-1 text-caption">
          <span className="mr-1.5 font-semibold">You</span>
          {row.text}
        </div>
      );
    case "text":
      return (
        <div className="rounded-md bg-well px-2.5 py-2 text-label-2 text-caption">
          <AgentMarkdown text={row.text} />
        </div>
      );
    case "thinking":
      return (
        <details className="rounded-md bg-well px-2.5 py-2 text-label-3 text-caption">
          <summary className="cursor-pointer select-none">Thinking</summary>
          <div className="mt-1 whitespace-pre-wrap font-mono">{row.text}</div>
        </details>
      );
    case "tool-call": {
      const parsed = typeof row.args === "string" ? tryParseJson(row.args) : undefined;
      const value = parsed ?? row.args;
      return (
        <div className="rounded-md bg-well px-2.5 py-2 text-caption">
          <ToolBadge toolName={row.toolName} act="call" />
          <div className="mt-1">
            <JsonTree
              value={value}
              onView={(text) => onView(`${row.toolName} args`, readableToolText(text), "plain")}
            />
          </div>
        </div>
      );
    }
    case "tool-return":
      return (
        <div className="rounded-md bg-well px-2.5 py-2 text-caption">
          <ToolBadge toolName={row.toolName} act="return" />
          <div className="mt-1">
            {typeof row.content === "string" ? (
              <ToolReturnText content={row.content} toolName={row.toolName} onView={onView} />
            ) : (
              <JsonTree
                value={row.content}
                onView={(text) => onView(`${row.toolName} return`, readableToolText(text), "plain")}
              />
            )}
          </div>
        </div>
      );
    case "retry-prompt":
      return (
        <div className="rounded-md bg-warn-soft px-2.5 py-2 text-warn-ink text-caption">
          <span className="font-semibold">Retry requested</span>
          {row.toolName !== null ? <span className="ml-1 font-mono">({row.toolName})</span> : null}
          <div className="mt-1 whitespace-pre-wrap font-mono">{row.text}</div>
        </div>
      );
    case "other":
      return (
        <div className="rounded-md bg-well px-2.5 py-2 text-caption">
          <span className="font-mono text-label-3">{row.label}</span>
          <div className="mt-1">
            <JsonTree value={row.value} onView={(text) => onView(row.label, text, "plain")} />
          </div>
        </div>
      );
    default:
      return assertNever(row);
  }
}
