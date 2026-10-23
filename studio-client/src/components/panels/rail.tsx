"use client";

import { cn } from "@/shared/lib/cn";
import { type ReactNode, type PointerEvent as ReactPointerEvent, useRef } from "react";

import { useStoredNumber } from "@/lib/use-stored";

// An IDE-style side drawer: resizable by its inner edge, collapsible to a slim
// labelled strip that reopens it, carrying a marker when the page has one to show.
// The open/closed state is the PAGE's (it also drives the canvas-maximize gesture);
// the width is the rail's own concern and persists per `storageKey`, so the layout
// somebody dragged into shape survives a reload.

export interface RailProps {
  side: "left" | "right";
  title: string;
  storageKey: string;
  open: boolean;
  onToggle: (open: boolean) => void;
  defaultWidth?: number;
  minWidth?: number;
  maxWidth?: number;
  // Shown on the folded strip, under the reopen control: a small sign that something inside is
  // worth opening for, a live run for instance. Nothing when the rail has nothing to say.
  marker?: ReactNode;
  children: ReactNode;
}

export function Rail({
  side,
  title,
  storageKey,
  open,
  onToggle,
  defaultWidth = 320,
  minWidth = 240,
  maxWidth = 560,
  marker = null,
  children,
}: RailProps) {
  // Persisted, but never read during render, see use-stored for why a
  // localStorage read in a `useState` initializer breaks hydration.
  const [width, setWidth] = useStoredNumber(`${storageKey}:width`, defaultWidth);
  const resizeRef = useRef<{ id: number; startX: number; startWidth: number } | null>(null);

  function beginResize(event: ReactPointerEvent<HTMLDivElement>) {
    resizeRef.current = { id: event.pointerId, startX: event.clientX, startWidth: width };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function resize(event: ReactPointerEvent<HTMLDivElement>) {
    const grip = resizeRef.current;
    if (!grip || event.pointerId !== grip.id) return;
    const delta = event.clientX - grip.startX;
    const next = Math.min(
      maxWidth,
      Math.max(minWidth, grip.startWidth + (side === "left" ? delta : -delta)),
    );
    setWidth(next);
  }

  function endResize(event: ReactPointerEvent<HTMLDivElement>) {
    // No write here: `setWidth` already persisted every step of the drag. The
    // write this replaced re-saved `width` from the render closure, which lags
    // the drag by a frame, a second source for one fact, and the stale one.
    if (resizeRef.current?.id !== event.pointerId) return;
    resizeRef.current = null;
  }

  if (!open) {
    return (
      <div
        className={cn(
          "flex w-9 shrink-0 flex-col items-center bg-surface py-2",
          side === "left" ? "border-line border-r" : "border-line border-l",
        )}
      >
        <button
          type="button"
          aria-label={`Open ${title}`}
          onClick={() => onToggle(true)}
          className="rounded p-1 text-label-3 hover:bg-well hover:text-label-2"
        >
          {side === "left" ? "▸" : "◂"}
        </button>
        {marker !== null ? <span className="mt-2 flex justify-center">{marker}</span> : null}
        <span
          className="mt-2 select-none text-[10px] text-label-3 uppercase tracking-widest"
          style={{ writingMode: "vertical-rl" }}
        >
          {title}
        </span>
      </div>
    );
  }

  const handle = (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${title}`}
      aria-valuemin={minWidth}
      aria-valuemax={maxWidth}
      aria-valuenow={width}
      tabIndex={0}
      onPointerDown={beginResize}
      onPointerMove={resize}
      onPointerUp={endResize}
      onPointerCancel={endResize}
      className="w-1 shrink-0 cursor-col-resize touch-none bg-line-strong transition-colors hover:bg-accent"
    />
  );

  return (
    <div className="flex shrink-0" style={{ width }}>
      {side === "right" ? handle : null}
      <div className="flex min-w-0 flex-1 flex-col bg-surface">
        <div className="flex items-center justify-between border-line border-b px-3 py-2">
          {/* A section heading, not a decorative label: the rail names a region of the page, and
              a station whose own heading is level one needs a level two under it for a reader
              moving by headings. */}
          <h2 className="font-medium text-xs text-label-2 uppercase tracking-wider">{title}</h2>
          <button
            type="button"
            aria-label={`Collapse ${title}`}
            onClick={() => onToggle(false)}
            className="rounded p-0.5 text-label-3 leading-none hover:bg-well hover:text-label-2"
          >
            {side === "left" ? "◂" : "▸"}
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </div>
      {side === "left" ? handle : null}
    </div>
  );
}
