"use client";

import { Loader2 } from "lucide-react";
import type { ReactElement } from "react";

import { compactCount, formatTurnElapsed } from "@/components/follow-me/vitals";
import { useNowTick } from "@/components/workflows/elapsed";

// The mark a turn wears while it is still working: a turning glyph, a verb that moves on as the
// seconds pass, and in parentheses how long it has been at it and how many tokens it has spent
// so far. What the mark knows is what the host hands it; a detail it does not have is left out.

export interface ThinkingDetails {
  /** When the turn started, ISO; `null` when the host has no clock for it. */
  startedAt: string | null;
  /** Output tokens spent since the turn started; `null` when the host has no count. */
  tokensOut: number | null;
}

const VERBS = ["Thinking", "Reading", "Pondering", "Working", "Weighing", "Musing", "Composing"];
const VERB_SECONDS = 6;

export function ThinkingMark({ startedAt, tokensOut }: ThinkingDetails): ReactElement {
  const now = useNowTick(1000, startedAt !== null);
  const elapsedMs = startedAt === null ? 0 : Math.max(0, now - Date.parse(startedAt));
  const verb = VERBS[Math.floor(elapsedMs / 1000 / VERB_SECONDS) % VERBS.length];
  const details = [
    startedAt === null ? null : formatTurnElapsed(elapsedMs),
    tokensOut === null ? null : `↓ ${compactCount(tokensOut)} tokens`,
  ].filter((part) => part !== null);

  return (
    <output aria-live="off" aria-label="Thinking" className="flex items-center gap-2 text-caption">
      <Loader2
        size={14}
        strokeWidth={1.75}
        aria-hidden
        data-motion="essential"
        className="animate-spin text-accent-ink"
      />
      <span data-motion="essential" className="animate-pulse font-medium text-accent-ink">
        {verb}
      </span>
      {details.length > 0 ? <span className="text-label-3">({details.join(", ")})</span> : null}
    </output>
  );
}
