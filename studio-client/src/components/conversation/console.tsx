"use client";

import { type ReactNode, useEffect, useRef, useState } from "react";

import type { Turn } from "@/api/studio-types";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui/button";
import { StatusDot } from "@/shared/ui/status-dot";
import { Textarea } from "@/shared/ui/textarea";

import { AgentMarkdown } from "./markdown";

// THE conversation surface: every place a person talks to the studio agent
// renders this one component — the Studio rail, the Harness drawer, wherever
// the agent speaks next. One bubble grammar, one live-stream treatment, one
// composer. Surfaces differ only through props: chrome titles, the empty-state
// invitation, and an optional footer (the Studio rail's draft strip).

export interface LiveTurn {
  user: string | null;
  text: string;
  act: string | null;
}

export interface ConsoleProps {
  title: string;
  turns: Turn[];
  thinking: boolean;
  live?: LiveTurn | null;
  error?: string | null;
  onSend: (text: string) => void;
  // Holds the composer shut while the surface has nothing to send a turn to yet, so a person
  // never types into a box whose message would have nowhere to go.
  sendDisabled?: boolean;
  onForget: () => void;
  forgetLabel?: string;
  forgetDisabled?: boolean;
  placeholder?: string;
  statusText?: { busy: string; idle: string };
  emptyState?: ReactNode;
  footer?: ReactNode;
  seed?: string;
  chrome?: "full" | "slim";
}

export function Console({
  title,
  turns,
  thinking,
  live = null,
  error = null,
  onSend,
  sendDisabled = false,
  onForget,
  forgetLabel = "Forget",
  forgetDisabled = false,
  placeholder = "Message the agent...",
  statusText = { busy: "working...", idle: "ready" },
  emptyState,
  footer,
  seed,
  chrome = "full",
}: ConsoleProps) {
  const [message, setMessage] = useState(seed ?? "");
  const logRef = useRef<HTMLDivElement | null>(null);

  // A scoped ask ("discuss this capability") arrives as a fresh seed: it replaces
  // whatever half-typed text the previous scope left, ready to edit before sending.
  useEffect(() => {
    if (seed !== undefined) setMessage(seed);
  }, [seed]);

  // Keep the newest exchange in view; a console that scrolls away from the
  // answer being written reads as frozen.
  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll tracks the turn and stream state
  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [turns.length, thinking, live?.text, live?.act]);

  function fire(text: string) {
    const trimmed = text.trim();
    if (!trimmed || thinking || sendDisabled) return;
    onSend(trimmed);
    setMessage("");
  }

  // Send on the physical Return key. Two independent paths on purpose: `keydown` is the normal
  // one, and `keyup` catches keystrokes whose keydown never reached the page, input-method
  // commits and extensions (Grammarly-style textarea overlays) both swallow keydown while keyup
  // still fires. A double fire is harmless: the keydown path clears the box, so the keyup path
  // sees empty text and no-ops.
  function sendOnEnter(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    const isEnter = event.code === "Enter" || event.code === "NumpadEnter" || event.key === "Enter";
    if (isEnter && !event.shiftKey) {
      event.preventDefault();
      fire(message);
    }
  }

  // The final structured answer arrives as raw JSON deltas under native output.
  // Prose streams as itself; the JSON settle renders as a quiet composing note
  // until the done event replaces it with the real message.
  const liveText = live?.text ?? "";
  const liveIsJson = liveText.trimStart().startsWith("{");

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* "slim" drops the title (a Rail or Drawer above already names the surface) and keeps
          only the status light and the forget act on one thin strip. */}
      <div className="flex items-center justify-between gap-2 border-line border-b px-3 py-2">
        <span className="flex min-w-0 items-center gap-2">
          <StatusDot tone="accent" pulse={thinking} className={cn(thinking && "glow-accent")} />
          {chrome === "full" ? (
            <span className="truncate font-medium text-caption text-label-1 uppercase tracking-wider">
              {title}
            </span>
          ) : null}
          <span className="text-label-3 text-micro">
            {thinking ? statusText.busy : statusText.idle}
          </span>
        </span>
        {turns.length > 0 ? (
          <Button
            variant="secondary"
            className="h-6 px-2 text-micro"
            onClick={onForget}
            disabled={forgetDisabled || thinking}
          >
            {forgetLabel}
          </Button>
        ) : null}
      </div>

      <div ref={logRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {turns.length === 0 && !thinking ? (
          (emptyState ?? null)
        ) : (
          <div className="flex flex-col gap-3">
            {turns.map((turn, index) => (
              <Bubble key={`${index}:${turn.who}`} who={turn.who} text={turn.text} />
            ))}
            {live?.user != null ? <Bubble who="you" text={live.user} /> : null}
            {thinking ? (
              <div className="max-w-[92%] self-start rounded-lg bg-well px-3 py-2 text-body text-label-2">
                {liveText !== "" && !liveIsJson ? (
                  <AgentMarkdown text={liveText} />
                ) : (
                  <span className="animate-pulse text-label-3">
                    {live?.act ?? (liveIsJson ? "writing the reply..." : "thinking...")}
                  </span>
                )}
                {live?.act != null && liveText !== "" && !liveIsJson ? (
                  <div className="mt-1 animate-pulse text-label-3 text-micro">{live.act}</div>
                ) : null}
              </div>
            ) : null}
          </div>
        )}
      </div>

      {error != null ? (
        <p
          role="alert"
          className="mx-3 mb-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {error}
        </p>
      ) : null}

      {footer}

      <div className="border-line border-t p-2.5">
        <div className="flex items-end gap-1.5">
          <Textarea
            aria-label="Message"
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={sendOnEnter}
            onKeyUp={sendOnEnter}
            rows={2}
            placeholder={placeholder}
            className="min-h-[3.25rem] flex-1 bg-well"
          />
          <Button
            onClick={() => fire(message)}
            disabled={thinking || sendDisabled || !message.trim()}
            className="h-8"
          >
            Send
          </Button>
        </div>
      </div>
    </div>
  );
}

function Bubble({ who, text }: { who: "you" | "agent"; text: string }) {
  return (
    <div
      className={cn(
        "max-w-[92%] rounded-lg px-3 py-2 text-body",
        who === "you" ? "self-end bg-accent-soft text-label-1" : "self-start bg-well text-label-2",
      )}
    >
      {who === "agent" ? <AgentMarkdown text={text} /> : text}
    </div>
  );
}
