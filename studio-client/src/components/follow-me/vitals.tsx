"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "client-core/ui/button";
import { ConfirmDialog } from "client-core/ui/confirm-dialog";
import { type ReactElement, useState } from "react";

import { sessionKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import type { SessionSummary } from "@/api/sessions-types";
import { spendText } from "@/lib/cost-text";

import { percentText, tokensText } from "./placement";

// The companion's own spend and context-window reading, and the control to start it over. The two
// arrive as separate pieces because the surface arranges them differently in different
// placements: the readout sits with the title, Clear sits with the controls.
//
// `masterId` names which session Clear acts on; the two query keys it invalidates are the same
// ones the surface reads the transcript and the detail by.

// A token count at the scale a reader can hold in their head: exact under a thousand, one
// decimal place through the thousands and millions, the decimal dropped where it would only
// read as ".0". Unlike `countText` in `./placement`, the decimal never drops above 10k -- this
// readout sits beside a live turn's elapsed clock, and a coarser rounding there would read as
// the number jumping rather than climbing.
export function compactCount(count: number): string {
  if (count < 1000) return String(Math.round(count));
  const scale = count < 1_000_000 ? 1000 : 1_000_000;
  const unit = count < 1_000_000 ? "k" : "M";
  const text = (count / scale).toFixed(1);
  return `${text.endsWith(".0") ? text.slice(0, -2) : text}${unit}`;
}

// The session's own token totals, compact enough to sit beside the cost and context readings.
export function tokensReadoutText(tokensIn: number, tokensOut: number): string {
  return `↑${compactCount(tokensIn)} ↓${compactCount(tokensOut)}`;
}

// The same totals in full, comma-grouped, for the accessible name -- the compact glyphs above
// read at a glance, this is what gets spoken.
export function tokensReadoutLabel(tokensIn: number, tokensOut: number): string {
  return `${tokensIn.toLocaleString("en-US")} tokens in, ${tokensOut.toLocaleString("en-US")} out`;
}

// A live turn's own duration, m:ss, ticking off the caller's clock rather than reading one of
// its own -- so every ticking readout the caller renders alongside it moves on the same instant.
// Non-finite reads empty (a clock still catching up to a just-opened turn); negative clamps to
// zero rather than going blank.
export function formatTurnElapsed(ms: number): string {
  if (!Number.isFinite(ms)) return "";
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export interface VitalsReadoutProps {
  costUsd: string;
  // How many of this session's responses no source could price, so the readout can say unknown
  // rather than show a figure that reads as everything the conversation cost.
  unpriced: number;
  context: SessionSummary["context"];
  // The session's own usage totals -- `session.usage.input_tokens` / `.output_tokens`.
  tokensIn: number;
  tokensOut: number;
  // ISO instant the in-flight turn began; `null` while no turn is running, so the elapsed
  // reading disappears rather than freezing on the last turn's number.
  turnStartedAt: string | null;
  // The caller's own ticking clock, read only while `turnStartedAt` is not null.
  now: number;
}

export interface ClearControlProps {
  masterId: string;
  // Called after a successful forget, so the caller can remount the chat engine --
  // clearing the transcript server-side doesn't by itself empty the on-screen thread.
  onCleared: () => void;
  // The confirm dialog's own title. Each mount of this control names what it is about to
  // restart, and it defaults to the companion's own wording.
  title?: string;
}

export function VitalsReadout({
  costUsd,
  unpriced,
  context,
  tokensIn,
  tokensOut,
  turnStartedAt,
  now,
}: VitalsReadoutProps): ReactElement {
  return (
    <div className="flex min-w-0 items-center gap-3 font-mono text-caption text-label-3">
      <span>{spendText(costUsd, unpriced)}</span>
      {context === null ? (
        <span>no reading</span>
      ) : (
        <>
          <span>{tokensText(context.used_tokens, context.window_tokens)}</span>
          <span>{percentText(context.fraction, context.resolved)}</span>
        </>
      )}
      <span
        role="img"
        aria-label={tokensReadoutLabel(tokensIn, tokensOut)}
        title={tokensReadoutLabel(tokensIn, tokensOut)}
      >
        {tokensReadoutText(tokensIn, tokensOut)}
      </span>
      {turnStartedAt !== null ? (
        <span>{formatTurnElapsed(now - new Date(turnStartedAt).getTime())}</span>
      ) : null}
    </div>
  );
}

export function ClearControl({
  masterId,
  onCleared,
  title = "Start the companion over?",
}: ClearControlProps): ReactElement {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);

  const forget = useMutation({
    mutationFn: () => sessionsApi.forget(masterId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: sessionKeys.transcript(masterId) });
      queryClient.invalidateQueries({ queryKey: sessionKeys.detail(masterId) });
      onCleared();
    },
    onSettled: () => setConfirming(false),
  });

  return (
    <>
      <Button
        variant="secondary"
        onClick={() => setConfirming(true)}
        className="h-7 shrink-0 px-3 text-caption"
      >
        Clear
      </Button>
      <ConfirmDialog
        open={confirming}
        title={title}
        message="The transcript stays on disk."
        confirmLabel="Clear"
        confirming={forget.isPending}
        onConfirm={() => forget.mutate()}
        onCancel={() => setConfirming(false)}
      />
    </>
  );
}
