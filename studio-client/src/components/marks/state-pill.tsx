import { cn } from "@/shared/lib/cn";
import type { ReactElement, ReactNode } from "react";

// A state is a shape plus a word, never a color alone. Tone picks the wash,
// border, ink, and icon together, so a caller cannot combine them into a
// state the design language does not have. size picks the pill scale (md
// for fleet rows and headers, sm for badge scale); the caller always
// supplies the word through children.

export type PillTone = "waiting" | "running" | "completed" | "failed" | "queued" | "warning";

export interface StatePillProps {
  tone: PillTone;
  size?: "md" | "sm";
  children: ReactNode;
  className?: string;
}

const TONE_CLASSES: Record<PillTone, string> = {
  waiting: "border border-warn bg-warn-soft text-warn-ink",
  running: "border border-accent bg-accent-soft text-accent-ink",
  completed: "border border-ok-ink bg-ok-soft text-ok-ink",
  failed:
    "border border-warn text-warn-ink bg-[repeating-linear-gradient(135deg,var(--warn-soft)_0_5px,var(--surface)_5px_10px)]",
  queued: "border border-dashed border-line-strong bg-well text-label-3",
  warning: "border border-warn bg-warn-soft text-warn-ink",
};

const SIZE_CLASSES: Record<"md" | "sm", string> = {
  md: "gap-1.5 py-1.75 pr-4 pl-3 text-caption",
  sm: "gap-1 py-1 pr-3 pl-2.25 text-micro",
};

const ICON_SIZE: Record<"md" | "sm", number> = {
  md: 13,
  sm: 10,
};

export function ToneIcon({ tone, size }: { tone: PillTone; size: number }): ReactElement | null {
  switch (tone) {
    case "waiting":
      return (
        <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <path d="M8 1.4l6.6 6.6L8 14.6 1.4 8z" />
        </svg>
      );
    case "warning":
      return (
        <svg
          width={size}
          height={size}
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.9}
          aria-hidden="true"
        >
          <path d="M8 1.8l6.4 11.4H1.6z" />
          <path d="M8 6.4v3M8 11.4h.01" />
        </svg>
      );
    case "running":
      return (
        <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <path d="M3.4 1.8l9.4 6.2-9.4 6.2z" />
        </svg>
      );
    case "completed":
      return (
        <svg
          width={size}
          height={size}
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth={2.3}
          aria-hidden="true"
        >
          <path d="M3.2 8.4l3.2 3.2 6.4-7" />
        </svg>
      );
    case "failed":
      return (
        <svg
          width={size}
          height={size}
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth={2.4}
          aria-hidden="true"
        >
          <path d="M3.6 3.6l8.8 8.8M12.4 3.6l-8.8 8.8" />
        </svg>
      );
    case "queued":
      return null;
  }
}

export function StatePill({
  tone,
  size = "md",
  children,
  className,
}: StatePillProps): ReactElement {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full font-semibold leading-none",
        SIZE_CLASSES[size],
        TONE_CLASSES[tone],
        className,
      )}
    >
      <ToneIcon tone={tone} size={ICON_SIZE[size]} />
      {children}
    </span>
  );
}
