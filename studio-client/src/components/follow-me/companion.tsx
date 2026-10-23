"use client";

import { useQuery } from "@tanstack/react-query";
import {
  AppWindow,
  Bot,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  GripVertical,
  PanelBottom,
  PanelLeft,
  PanelRight,
} from "lucide-react";
import {
  type CSSProperties,
  type ReactElement,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  useRef,
  useState,
} from "react";

import { sessionKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import { studioErrorMessage } from "@/api/studio";
import type { ChatReply } from "@/api/studio-types";
import { SessionChat } from "@/components/chat/session-chat";
import { useStoredChoice, useStoredFlag, useStoredJson, useStoredNumber } from "@/lib/use-stored";
import { cn } from "@/shared/lib/cn";

import {
  HEIGHT,
  HEIGHT_KEY,
  NAV_KEY,
  PINNED_KEY,
  PLACEMENTS,
  PLACEMENT_KEY,
  type Placement,
  RESIZE_STEP,
  WIDTH,
  WIDTH_KEY,
  WINDOW_H,
  WINDOW_INSET,
  WINDOW_KEY,
  WINDOW_W,
  type WindowRect,
  clamp,
  clampWindowRect,
  parseWindowRect,
} from "./placement";
import { useFollowMe } from "./provider";
import { useDrag } from "./use-drag";
import { useVerbExecutor } from "./verbs";
import { ClearControl, VitalsReadout } from "./vitals";

// The companion: one conversation over the standing "master" session, page-aware through
// `useFollowMe`, that a viewer can put on either rail, along the bottom, or in a window floating
// over the page. Collapsed it is a single control named "Companion" wherever it sits.
//
// The master session mints lazily: the query that resolves it only runs once pinned, never on a
// plain page load. The left placement, and only it, pairs with the nav -- pinning there collapses
// the nav, remembering what it was right before, so leaving restores that value rather than
// always expanding it. A surface that mounts already pinned never captured a prior value, so it
// restores to expanded.
//
// Sizes persist per viewer and are read back through their ranges, so nothing on disk can render
// a surface too small to use. A drag writes on release, not on every frame.

export interface CompanionProps {
  // The page column a floating window lives inside, so a drag can be held within it.
  columnRef: RefObject<HTMLDivElement | null>;
}

interface SwitcherOption {
  value: Placement;
  label: string;
  Icon: typeof PanelLeft;
}

const SWITCHER: readonly SwitcherOption[] = [
  { value: "left", label: "Left rail", Icon: PanelLeft },
  { value: "right", label: "Right rail", Icon: PanelRight },
  { value: "bottom", label: "Bottom panel", Icon: PanelBottom },
  { value: "floating", label: "Floating window", Icon: AppWindow },
];

const COLLAPSE_ICON = {
  left: ChevronLeft,
  right: ChevronRight,
  bottom: ChevronDown,
  floating: ChevronDown,
} as const;

const CHIP = "absolute z-10 rounded-md bg-label-1 px-1.5 py-1 font-mono text-[11px] text-surface";

// Holds a coordinate inside an extent that is only known once the page column has been laid out;
// an unmeasured column bounds nothing, so the value only has to stay off the near edge.
function held(value: number, size: number, extent: number): number {
  const room = extent > 0 ? Math.max(0, extent - size) : Number.POSITIVE_INFINITY;
  return clamp(value, 0, room);
}

function PlacementSwitcher({
  placement,
  onSelect,
}: {
  placement: Placement;
  onSelect: (next: Placement) => void;
}): ReactElement {
  return (
    <div className="flex shrink-0 items-center gap-0.5 rounded-md border border-line bg-well p-0.5">
      {SWITCHER.map(({ value, label, Icon }) => (
        <button
          key={value}
          type="button"
          aria-label={label}
          aria-pressed={placement === value}
          onClick={() => onSelect(value)}
          className={cn(
            "flex size-[26px] items-center justify-center rounded-[8px]",
            placement === value
              ? "bg-label-1 text-surface"
              : "text-label-3 hover:bg-surface hover:text-label-1",
          )}
        >
          <Icon aria-hidden size={16} strokeWidth={1.8} />
        </button>
      ))}
    </div>
  );
}

export function Companion({ columnRef }: CompanionProps): ReactElement {
  const [pinned, setPinned] = useStoredFlag(PINNED_KEY, false);
  const [placement, setPlacement] = useStoredChoice<Placement>(PLACEMENT_KEY, "left", PLACEMENTS);
  const [navCollapsed, setNavCollapsed] = useStoredFlag(NAV_KEY, false);
  const [storedWidth, setStoredWidth] = useStoredNumber(WIDTH_KEY, WIDTH.fallback);
  const [storedHeight, setStoredHeight] = useStoredNumber(HEIGHT_KEY, HEIGHT.fallback);
  const [storedRect, setStoredRect] = useStoredJson<WindowRect | null>(
    WINDOW_KEY,
    null,
    parseWindowRect,
  );
  const [liveWidth, setLiveWidth] = useState<number | null>(null);
  const [liveHeight, setLiveHeight] = useState<number | null>(null);
  const [liveRect, setLiveRect] = useState<WindowRect | null>(null);
  const [reply, setReply] = useState<ChatReply | null>(null);
  // Bumped on a successful Clear, and folded into the chat engine's key below: the engine seeds
  // its messages once, at creation, so emptying the transcript server-side only reaches the
  // screen once this remounts it.
  const [clearedAt, setClearedAt] = useState(0);
  const { collectContext } = useFollowMe();
  const executeTool = useVerbExecutor();
  // What the nav's collapsed flag read right before this instance pinned it, so a collapse can
  // put it back; null when this instance never collapsed it itself.
  const navBeforePinRef = useRef<boolean | null>(null);
  // What a gesture started from, and where it has reached. The pending values are refs, not
  // state, so the release writes exactly what the last move produced.
  const baseWidthRef = useRef(WIDTH.fallback);
  const baseHeightRef = useRef(HEIGHT.fallback);
  const baseRectRef = useRef<WindowRect | null>(null);
  const pendingWidthRef = useRef<number | null>(null);
  const pendingHeightRef = useRef<number | null>(null);
  const pendingRectRef = useRef<WindowRect | null>(null);

  const width = liveWidth ?? clamp(storedWidth, WIDTH.min, WIDTH.max);
  const height = liveHeight ?? clamp(storedHeight, HEIGHT.min, HEIGHT.max);
  const rect = liveRect ?? (storedRect === null ? null : clampWindowRect(storedRect));

  const masterQuery = useQuery({
    queryKey: ["master"],
    queryFn: sessionsApi.master,
    staleTime: Number.POSITIVE_INFINITY,
    enabled: pinned,
  });
  const masterId = masterQuery.data;
  const errorTitle = masterQuery.isError ? studioErrorMessage(masterQuery.error) : undefined;

  const detailQuery = useQuery({
    queryKey: sessionKeys.detail(masterId ?? null),
    queryFn: () => sessionsApi.detail(masterId ?? ""),
    refetchInterval: 5000,
    enabled: pinned && masterId !== undefined,
  });

  function collapseNav(): void {
    navBeforePinRef.current = navCollapsed;
    setNavCollapsed(true);
  }

  function restoreNav(): void {
    setNavCollapsed(navBeforePinRef.current ?? false);
    navBeforePinRef.current = null;
  }

  function pin(): void {
    if (placement === "left") collapseNav();
    setPinned(true);
  }

  function collapse(): void {
    if (placement === "left") restoreNav();
    setPinned(false);
  }

  function changePlacement(next: Placement): void {
    if (next === placement) return;
    if (pinned && placement === "left") restoreNav();
    if (pinned && next === "left") collapseNav();
    setPlacement(next);
  }

  // Where a floating window is right now. With nothing stored it rests in the page column's
  // bottom right corner, which is what a first drag has to start from.
  function currentRect(): WindowRect {
    if (rect !== null) return rect;
    const column = columnRef.current;
    const w = WINDOW_W.fallback;
    const h = WINDOW_H.fallback;
    return {
      x: Math.max(0, (column?.clientWidth ?? 0) - w - WINDOW_INSET),
      y: Math.max(0, (column?.clientHeight ?? 0) - h - WINDOW_INSET),
      w,
      h,
    };
  }

  const widthDrag = useDrag({
    onStart: () => {
      baseWidthRef.current = width;
    },
    onMove: (dx) => {
      const travel = placement === "left" ? dx : -dx;
      const next = clamp(baseWidthRef.current + travel, WIDTH.min, WIDTH.max);
      pendingWidthRef.current = next;
      setLiveWidth(next);
    },
    onEnd: () => {
      const next = pendingWidthRef.current;
      if (next !== null) setStoredWidth(next);
      pendingWidthRef.current = null;
      setLiveWidth(null);
    },
  });

  const heightDrag = useDrag({
    onStart: () => {
      baseHeightRef.current = height;
    },
    onMove: (_dx, dy) => {
      const next = clamp(baseHeightRef.current - dy, HEIGHT.min, HEIGHT.max);
      pendingHeightRef.current = next;
      setLiveHeight(next);
    },
    onEnd: () => {
      const next = pendingHeightRef.current;
      if (next !== null) setStoredHeight(next);
      pendingHeightRef.current = null;
      setLiveHeight(null);
    },
  });

  function commitRect(): void {
    const next = pendingRectRef.current;
    if (next !== null) setStoredRect(next);
    baseRectRef.current = null;
    pendingRectRef.current = null;
    setLiveRect(null);
  }

  const moveDrag = useDrag({
    onStart: () => {
      baseRectRef.current = currentRect();
    },
    onMove: (dx, dy) => {
      const base = baseRectRef.current;
      if (base === null) return;
      const column = columnRef.current;
      const next: WindowRect = {
        ...base,
        x: held(base.x + dx, base.w, column?.clientWidth ?? 0),
        y: held(base.y + dy, base.h, column?.clientHeight ?? 0),
      };
      pendingRectRef.current = next;
      setLiveRect(next);
    },
    onEnd: commitRect,
  });

  const sizeDrag = useDrag({
    onStart: () => {
      baseRectRef.current = currentRect();
    },
    onMove: (dx, dy) => {
      const base = baseRectRef.current;
      if (base === null) return;
      const next: WindowRect = {
        ...base,
        w: clamp(base.w + dx, WINDOW_W.min, WINDOW_W.max),
        h: clamp(base.h + dy, WINDOW_H.min, WINDOW_H.max),
      };
      pendingRectRef.current = next;
      setLiveRect(next);
    },
    onEnd: commitRect,
  });

  function onWidthKey(event: ReactKeyboardEvent<HTMLElement>): void {
    const grow = placement === "left" ? "ArrowRight" : "ArrowLeft";
    const shrink = placement === "left" ? "ArrowLeft" : "ArrowRight";
    if (event.key !== grow && event.key !== shrink) return;
    event.preventDefault();
    const step = event.key === grow ? RESIZE_STEP : -RESIZE_STEP;
    setStoredWidth(clamp(width + step, WIDTH.min, WIDTH.max));
  }

  function onHeightKey(event: ReactKeyboardEvent<HTMLElement>): void {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const step = event.key === "ArrowUp" ? RESIZE_STEP : -RESIZE_STEP;
    setStoredHeight(clamp(height + step, HEIGHT.min, HEIGHT.max));
  }

  function onCornerKey(event: ReactKeyboardEvent<HTMLElement>): void {
    const dw =
      event.key === "ArrowRight" ? RESIZE_STEP : event.key === "ArrowLeft" ? -RESIZE_STEP : 0;
    const dh = event.key === "ArrowDown" ? RESIZE_STEP : event.key === "ArrowUp" ? -RESIZE_STEP : 0;
    if (dw === 0 && dh === 0) return;
    event.preventDefault();
    const base = currentRect();
    setStoredRect({
      ...base,
      w: clamp(base.w + dw, WINDOW_W.min, WINDOW_W.max),
      h: clamp(base.h + dh, WINDOW_H.min, WINDOW_H.max),
    });
  }

  // The window follows its title row, except where that row's own controls were pressed.
  function beginMove(event: ReactPointerEvent<HTMLElement>): void {
    if (event.target instanceof Element && event.target.closest("button") !== null) return;
    moveDrag.handlers.onPointerDown(event);
  }

  if (!pinned) {
    if (placement === "bottom") {
      return (
        <button
          type="button"
          aria-label="Companion"
          title={errorTitle}
          onClick={pin}
          className="flex h-8 shrink-0 items-center gap-2 border-line border-t bg-surface px-4 text-label-3 hover:text-label-1"
        >
          <Bot aria-hidden size={16} strokeWidth={1.8} />
          <span className="font-semibold text-caption text-label-3">Companion</span>
          <ChevronUp aria-hidden size={16} strokeWidth={1.8} />
        </button>
      );
    }
    if (placement === "floating") {
      return (
        <button
          type="button"
          aria-label="Companion"
          title={errorTitle}
          onClick={pin}
          className="absolute right-4 bottom-4 z-30 flex size-11 items-center justify-center rounded-full bg-label-1 text-surface shadow-lg"
        >
          <Bot aria-hidden size={16} strokeWidth={1.8} />
        </button>
      );
    }
    return (
      <div
        className={cn(
          "flex h-full w-10 shrink-0 flex-col items-center bg-surface pt-4",
          placement === "left" ? "border-line border-r" : "border-line border-l",
        )}
      >
        <button
          type="button"
          aria-label="Companion"
          title={errorTitle}
          onClick={pin}
          className="rounded-md p-1.5 text-label-3 hover:bg-well hover:text-label-1"
        >
          <Bot aria-hidden size={16} strokeWidth={1.8} />
        </button>
      </div>
    );
  }

  const session = detailQuery.data?.session;
  const CollapseIcon = COLLAPSE_ICON[placement];

  const controls = (
    <>
      <PlacementSwitcher placement={placement} onSelect={changePlacement} />
      <button
        type="button"
        aria-label="Collapse"
        onClick={collapse}
        className="shrink-0 rounded-md p-1 text-label-3 hover:bg-well hover:text-label-1"
      >
        <CollapseIcon aria-hidden size={16} strokeWidth={1.8} />
      </button>
    </>
  );

  const readout =
    session !== undefined ? (
      <VitalsReadout
        costUsd={session.cost_usd}
        unpriced={session.unpriced}
        context={session.context}
        tokensIn={session.usage.input_tokens}
        tokensOut={session.usage.output_tokens}
        // The companion has no turn-in-flight clock of its own to thread in here: it never
        // shows the elapsed reading, only the totals the session detail already carries.
        turnStartedAt={null}
        now={0}
      />
    ) : null;
  const clear =
    session !== undefined ? (
      <ClearControl
        masterId={masterId ?? ""}
        onCleared={() => setClearedAt((count) => count + 1)}
      />
    ) : null;

  const body: ReactNode =
    masterId !== undefined ? (
      <div className="flex min-h-0 flex-1 flex-col">
        <SessionChat
          key={`${masterId}:${clearedAt}`}
          sessionId={masterId}
          reply={reply}
          onSettle={setReply}
          getContext={collectContext}
          executeTool={executeTool}
        />
      </div>
    ) : masterQuery.isError ? (
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-1 bg-ground px-4 text-center text-body text-danger-ink">
        <p>Could not load the companion.</p>
        <p className="text-label-3">{errorTitle}</p>
      </div>
    ) : (
      <div className="flex min-h-0 flex-1 items-center justify-center bg-ground text-body text-label-3">
        Loading the companion.
      </div>
    );

  const titleRow = (
    <div
      data-testid={placement === "floating" ? "companion-handle" : undefined}
      {...(placement === "floating"
        ? { ...moveDrag.handlers, onPointerDown: beginMove }
        : undefined)}
      className={cn(
        "flex shrink-0 items-center justify-between gap-2.5 border-line border-b px-4 py-3",
        placement === "floating" && "cursor-grab touch-none",
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        {placement === "floating" ? (
          <GripVertical aria-hidden size={16} strokeWidth={1.8} className="text-line-strong" />
        ) : null}
        <Bot aria-hidden size={16} strokeWidth={1.8} className="shrink-0 text-label-2" />
        <span className="font-semibold text-body text-label-1">Companion</span>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">{controls}</div>
    </div>
  );

  const vitalsRow =
    session !== undefined ? (
      <div className="flex shrink-0 items-center justify-between gap-3 border-line border-b bg-well px-4 py-1.5">
        {readout}
        {clear}
      </div>
    ) : null;

  if (placement === "bottom") {
    const heightGrip = (
      // biome-ignore lint/a11y/useSemanticElements: an <hr> takes neither the grip's own pill nor a pointer drag
      <div
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize"
        aria-valuemin={HEIGHT.min}
        aria-valuemax={HEIGHT.max}
        aria-valuenow={height}
        tabIndex={0}
        onKeyDown={onHeightKey}
        {...heightDrag.handlers}
        className="relative flex h-2 shrink-0 cursor-row-resize touch-none items-center justify-center"
      >
        <span aria-hidden className="h-[3px] w-9 rounded-full bg-line-strong" />
        {heightDrag.dragging ? (
          <span className={cn(CHIP, "bottom-full mb-1")}>{height}</span>
        ) : null}
      </div>
    );

    return (
      <div
        title={errorTitle}
        style={{ height }}
        className="relative flex shrink-0 flex-col border-line border-t bg-surface"
      >
        {heightGrip}
        <div className="flex shrink-0 items-center gap-3 border-line border-b px-4 py-2">
          <Bot aria-hidden size={16} strokeWidth={1.8} className="shrink-0 text-label-2" />
          <span className="font-semibold text-body text-label-1">Companion</span>
          {readout}
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            {clear}
            {controls}
          </div>
        </div>
        {body}
      </div>
    );
  }

  if (placement === "floating") {
    const style: CSSProperties =
      rect === null
        ? {
            right: WINDOW_INSET,
            bottom: WINDOW_INSET,
            width: WINDOW_W.fallback,
            height: WINDOW_H.fallback,
          }
        : { left: rect.x, top: rect.y, width: rect.w, height: rect.h };

    return (
      <div
        title={errorTitle}
        style={style}
        className="absolute z-30 flex flex-col overflow-hidden rounded-lg border border-line-strong bg-surface shadow-lg"
      >
        {titleRow}
        {vitalsRow}
        {body}
        <div
          role="separator"
          aria-label="Resize"
          aria-valuemin={WINDOW_W.min}
          aria-valuemax={WINDOW_W.max}
          aria-valuenow={rect?.w ?? WINDOW_W.fallback}
          tabIndex={0}
          onKeyDown={onCornerKey}
          {...sizeDrag.handlers}
          className="absolute right-0 bottom-0 size-4 cursor-nwse-resize touch-none"
        />
      </div>
    );
  }

  const widthGrip = (
    // biome-ignore lint/a11y/useSemanticElements: an <hr> takes neither the grip's own pill nor a pointer drag
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize"
      aria-valuemin={WIDTH.min}
      aria-valuemax={WIDTH.max}
      aria-valuenow={width}
      tabIndex={0}
      onKeyDown={onWidthKey}
      {...widthDrag.handlers}
      className="relative flex w-2 shrink-0 cursor-col-resize touch-none items-center justify-center"
    >
      <span aria-hidden className="h-9 w-[3px] rounded-full bg-line-strong" />
      {widthDrag.dragging ? (
        <span className={cn(CHIP, placement === "left" ? "left-full ml-1" : "right-full mr-1")}>
          {width}
        </span>
      ) : null}
    </div>
  );

  return (
    <div
      title={errorTitle}
      style={{ width }}
      className={cn(
        "flex shrink-0 bg-surface",
        placement === "left" ? "border-line border-r" : "border-line border-l",
      )}
    >
      {placement === "right" ? widthGrip : null}
      <div className="flex min-w-0 flex-1 flex-col">
        {titleRow}
        {vitalsRow}
        {body}
      </div>
      {placement === "left" ? widthGrip : null}
    </div>
  );
}
