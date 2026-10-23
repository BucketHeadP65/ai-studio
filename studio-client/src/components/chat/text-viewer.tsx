"use client";

import type { ReactElement } from "react";

import { MessagePayloadSchema } from "@/api/sessions-types";
import { AgentMarkdown } from "@/components/conversation/markdown";
import { Drawer } from "@/shared/ui/drawer";

// A long string (a tool return, a spec, a skill body) viewed on its own, past
// the point a chat bubble or a JsonTree row can show it inline. Markdown by
// default; a caller that knows the text isn't prose passes mode "plain" for a
// verbatim, monospaced view instead. Mode "conversation" is for a worker
// step's own message history instead of a single string: it renders
// `messages` as alternating owner/worker bubbles.

interface TextViewerProps {
  open: boolean;
  title: string;
  text?: string;
  mode?: "markdown" | "plain" | "conversation";
  messages?: unknown[];
  onClose: () => void;
}

interface Bubble {
  role: "owner" | "worker";
  text: string;
}

// Each entry in a worker's own message list is pydantic-ai's `ModelMessage` grammar dumped
// to JSON: a request carries the owner's turn in its user-prompt parts, a response carries
// the worker's own turn in its text parts. Parsed loosely, so an unrendered field or an
// unlisted part kind never breaks the drawer; a message that renders no text (a tool call,
// a tool return) contributes no bubble.
function toBubbles(messages: unknown[]): Bubble[] {
  const bubbles: Bubble[] = [];
  for (const raw of messages) {
    const parsed = MessagePayloadSchema.safeParse(raw);
    if (!parsed.success) continue;
    const isRequest = parsed.data.kind === "request";
    const wantedPartKind = isRequest ? "user-prompt" : "text";
    const texts: string[] = [];
    for (const part of parsed.data.parts) {
      if (part.part_kind === wantedPartKind && typeof part.content === "string") {
        texts.push(part.content);
      }
    }
    const text = texts.join("");
    if (text === "") continue;
    bubbles.push({ role: isRequest ? "owner" : "worker", text });
  }
  return bubbles;
}

export function TextViewer({
  open,
  title,
  text = "",
  mode = "markdown",
  messages,
  onClose,
}: TextViewerProps): ReactElement | null {
  return (
    <Drawer open={open} title={title} onClose={onClose}>
      <div className="h-full overflow-y-auto px-4 py-3">
        {mode === "conversation" ? (
          <div className="space-y-2">
            {toBubbles(messages ?? []).map((bubble, index) => (
              <div
                key={`${index}-${bubble.role}`}
                className={
                  bubble.role === "owner"
                    ? "ml-auto max-w-[85%] rounded-md bg-accent-soft p-2.5 text-body"
                    : "max-w-[85%] rounded-md bg-well p-2.5 text-body"
                }
              >
                <p className="text-label-3 text-micro font-semibold uppercase tracking-caps">
                  {bubble.role}
                </p>
                <AgentMarkdown text={bubble.text} />
              </div>
            ))}
          </div>
        ) : mode === "plain" ? (
          <pre className="whitespace-pre-wrap rounded-md border border-line bg-well p-2.5 font-mono text-label-2 text-caption">
            {text}
          </pre>
        ) : (
          <AgentMarkdown text={text} />
        )}
      </div>
    </Drawer>
  );
}
