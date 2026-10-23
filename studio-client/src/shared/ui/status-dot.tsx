import type { HTMLAttributes } from "react";
import { cn } from "../lib/cn";

type Tone = "ok" | "warn" | "danger" | "accent" | "muted";

interface Props extends HTMLAttributes<HTMLSpanElement> {
  tone?: Tone;
  pulse?: boolean;
}

// The presence light: alive, busy, broken. Decorative beside its label, so it
// is aria-hidden; the label carries the meaning for assistive tech.
const TONE_CLASSES: Record<Tone, string> = {
  ok: "bg-ok-ink",
  warn: "bg-warn-ink",
  danger: "bg-danger-ink",
  accent: "bg-accent",
  muted: "bg-label-3",
};

export function StatusDot({ tone = "muted", pulse = false, className, ...rest }: Props) {
  return (
    <span
      aria-hidden
      className={cn(
        "inline-block h-2 w-2 shrink-0 rounded-full",
        TONE_CLASSES[tone],
        pulse && "animate-pulse",
        className,
      )}
      {...rest}
    />
  );
}
