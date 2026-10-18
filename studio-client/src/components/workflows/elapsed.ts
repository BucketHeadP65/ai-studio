"use client";

import { useEffect, useState } from "react";

// A live duration in mono, coarsest unit first: hours and minutes once there are any hours,
// minutes and seconds once there are any minutes, seconds alone otherwise. A non-finite input
// (a clock still catching up to a just-started timestamp) reads as empty rather than a nonsense
// duration; a negative one clamps to zero rather than going blank.
export function formatElapsedMs(ms: number): string {
  if (!Number.isFinite(ms)) return "";
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

// The current time, refreshed every `intervalMs` while `active`, so a value derived from it (an
// elapsed duration) ticks on screen without polling the network for it. Idle when `active` is
// false, for a duration that has already stopped changing (an ended run).
export function useNowTick(intervalMs = 1000, active = true): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, active]);

  return now;
}
