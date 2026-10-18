"use client";

import { type PointerEvent as ReactPointerEvent, useRef, useState } from "react";

// One pointer gesture, shared by every grip the companion has: the press fixes an origin, each
// move reports how far the pointer has travelled from it, and the release closes the gesture.
// Deltas rather than positions, so a caller adds them to whatever value it captured at the press
// and never has to know where on the page the grip is.
//
// The pointer is captured on press, so a fast drag that outruns the grip keeps arriving here
// rather than at whatever the pointer happens to be over.

export interface DragCallbacks {
  // Fires on the press, before any move, so a caller can snapshot what it is about to change.
  onStart?: () => void;
  onMove: (dx: number, dy: number) => void;
  onEnd?: () => void;
}

export interface DragHandlers {
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerMove: (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerUp: (event: ReactPointerEvent<HTMLElement>) => void;
  onPointerCancel: (event: ReactPointerEvent<HTMLElement>) => void;
}

export interface Drag {
  dragging: boolean;
  handlers: DragHandlers;
}

interface Origin {
  id: number;
  x: number;
  y: number;
}

export function useDrag({ onStart, onMove, onEnd }: DragCallbacks): Drag {
  const originRef = useRef<Origin | null>(null);
  const [dragging, setDragging] = useState(false);

  function start(event: ReactPointerEvent<HTMLElement>): void {
    originRef.current = { id: event.pointerId, x: event.clientX, y: event.clientY };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setDragging(true);
    onStart?.();
  }

  function follow(event: ReactPointerEvent<HTMLElement>): void {
    const origin = originRef.current;
    if (origin === null || origin.id !== event.pointerId) return;
    onMove(event.clientX - origin.x, event.clientY - origin.y);
  }

  function finish(event: ReactPointerEvent<HTMLElement>): void {
    const origin = originRef.current;
    if (origin === null || origin.id !== event.pointerId) return;
    originRef.current = null;
    setDragging(false);
    onEnd?.();
  }

  return {
    dragging,
    handlers: {
      onPointerDown: start,
      onPointerMove: follow,
      onPointerUp: finish,
      onPointerCancel: finish,
    },
  };
}
