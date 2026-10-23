import type { HTMLAttributes } from "react";
import { cn } from "../lib/cn";

type Tone = "neutral" | "ok" | "warn" | "danger" | "accent";

interface Props extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
}

// A small state label. Tones map to the consuming app's semantic soft/ink
// pairs, so a badge can never invent a color the theme does not carry.
const TONE_CLASSES: Record<Tone, string> = {
  neutral: "bg-muted-soft text-label-2",
  ok: "bg-ok-soft text-ok-ink",
  warn: "bg-warn-soft text-warn-ink",
  danger: "bg-danger-soft text-danger-ink",
  accent: "bg-accent-soft text-accent",
};

export function Badge({ tone = "neutral", className, ...rest }: Props) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 font-medium text-micro uppercase tracking-wide",
        TONE_CLASSES[tone],
        className,
      )}
      {...rest}
    />
  );
}
