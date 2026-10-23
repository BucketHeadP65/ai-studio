"use client";

import { ArrowUp, Square } from "lucide-react";
import {
  type KeyboardEvent,
  type ReactElement,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";

import type { GateMode } from "@/api/studio-types";
import { useTargetRegistration } from "@/components/follow-me/targets";
import { GATE_MODE_WORDS } from "@/components/marks/gate-mode";
import { Textarea } from "@/shared/ui/textarea";

// The chat input row: send a message, and stop a running answer. Presentational only, every act
// it takes arrives through a prop, it fetches nothing itself. Stays usable while a gate waits --
// the gate turn in the transcript is where that gets answered, not here.

// The message being written, and the box it is written in.
export interface ComposerDraft {
  text: string;
  onChange: (text: string) => void;
  boxRef: RefObject<HTMLTextAreaElement | null>;
}

// Holds a draft above the composer, and registers it as a companion target under `target` when
// one is given. The registration outlives the box itself, so a prefill that arrives while
// something else holds the composer's place lands in the box the person gets back.
export function useComposerDraft(target?: string): ComposerDraft {
  const [text, setText] = useState("");
  const boxRef = useRef<HTMLTextAreaElement | null>(null);

  useTargetRegistration(
    target ? { name: target, kind: "input", description: "The message box" } : null,
    {
      element: () => boxRef.current,
      prefill: (drafted) => {
        setText(drafted);
        boxRef.current?.focus();
      },
    },
  );

  return { text, onChange: setText, boxRef };
}

export interface ComposerProps {
  onSend: (text: string) => void;
  busy: boolean;
  onStop: (() => void) | null;
  notice: string | null;
  // The run's own gate mode, said quietly in the controls row. Absent, no caption renders -- a
  // caller with no gate of its own (the step chat drawer steers a step, not a run) rather than
  // showing a word about a gate that is not there.
  gateMode?: GateMode;
  draft: ComposerDraft;
  // The model answering here, said quietly in the controls row; null says nothing.
  model?: string | null;
}

const MAX_ROWS = 8;
const FALLBACK_LINE_HEIGHT = 24;

export function Composer({
  onSend,
  busy,
  onStop,
  notice,
  gateMode,
  draft,
  model = null,
}: ComposerProps): ReactElement {
  const { text: message, onChange, boxRef: textareaRef } = draft;

  // Grows the box with the text from one row up to eight, then lets it scroll instead of
  // pushing the rest of the layout around.
  // biome-ignore lint/correctness/useExhaustiveDependencies: height tracks the message text
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    if (el.scrollHeight > 0) {
      const lineHeight = Number.parseFloat(getComputedStyle(el).lineHeight);
      const maxHeight =
        (Number.isFinite(lineHeight) ? lineHeight : FALLBACK_LINE_HEIGHT) * MAX_ROWS;
      el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
    }
  }, [message]);

  function fire() {
    const trimmed = message.trim();
    if (!trimmed || busy) return;
    onSend(trimmed);
    onChange("");
  }

  // Send on the physical Return key, Shift+Return keeps its newline. Both keydown
  // and keyup run the same guarded send so a swallowed keydown (input methods,
  // extension overlays) still sends, the keydown path already cleared the box, so
  // the keyup path sees empty text and no-ops.
  function sendOnEnter(event: KeyboardEvent<HTMLTextAreaElement>) {
    const isEnter = event.code === "Enter" || event.code === "NumpadEnter" || event.key === "Enter";
    if (isEnter && !event.shiftKey) {
      event.preventDefault();
      fire();
    }
  }

  // One card the height of its text: a single line until the person writes more, the controls
  // in a slim row underneath, the send as an icon at the right where the eye lands. The card
  // shares the conversation column's width, so the words land where the reply will.
  return (
    <div className="mx-auto w-full max-w-[1400px] px-4 pt-2 pb-3">
      <div className="rounded-2xl border border-line bg-surface px-3 pt-2.5 pb-1.5 shadow-sm focus-within:border-line-strong">
        <Textarea
          ref={textareaRef}
          aria-label="Message"
          value={message}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={sendOnEnter}
          onKeyUp={sendOnEnter}
          rows={1}
          disabled={busy}
          placeholder="Message the agent"
          className="block border-0 bg-transparent px-0 py-0 text-body leading-6 focus:border-0 focus:outline-none"
        />
        <div className="mt-1 flex h-7 items-center gap-2">
          <span className="mr-auto truncate text-caption text-label-3">
            {gateMode ? GATE_MODE_WORDS[gateMode].hint : null}
          </span>
          {model ? <span className="truncate text-caption text-label-3">{model}</span> : null}
          {onStop ? (
            <button
              type="button"
              aria-label="Stop"
              title="Stop"
              onClick={onStop}
              className="flex h-7 w-7 items-center justify-center rounded-full text-label-2 transition-colors hover:bg-well"
            >
              <Square className="h-3.5 w-3.5" fill="currentColor" aria-hidden="true" />
            </button>
          ) : null}
          <button
            type="button"
            aria-label="Send"
            title="Send (Return)"
            onClick={fire}
            disabled={busy || !message.trim()}
            className="flex h-7 w-7 items-center justify-center rounded-full bg-label-1 text-surface transition-colors hover:bg-label-1/85 disabled:cursor-not-allowed disabled:bg-muted-soft disabled:text-label-3"
          >
            <ArrowUp className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>
      {notice ? (
        <output className="mt-1.5 block text-caption text-warn-ink">{notice}</output>
      ) : null}
    </div>
  );
}
