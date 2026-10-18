"use client";

import { useCallback, useEffect, useState } from "react";

// UI preferences that outlive a reload — which rails are open, how wide they
// are — without breaking hydration.
//
// The obvious implementation reads localStorage in the `useState` initializer,
// and it is wrong: the server has no localStorage and renders the fallback,
// the browser reads a stored value and renders something else, and React
// discards the whole tree with "Hydration failed because the server rendered
// HTML didn't match the client". It is invisible until someone changes a
// preference — the studio's chat rail had been collapsed once, and every page
// load after that hydration-errored.
//
// So: render the fallback, always, on both sides; adopt the stored value in an
// effect, which runs only in the browser and only after the two renders have
// already agreed. The cost is one extra paint for a reader who changed a
// default; the alternative is a broken tree for all of them.

const NAMESPACE = "studio:";

// Two mounted hooks on the same key must agree within a tab -- the setter below writes through
// `localStorage`, but the browser's own `storage` event fires only in OTHER tabs, never the one
// that made the write. This event closes that gap for listeners in the same tab.
const SYNC_EVENT = "studio:stored";

interface StoredSyncDetail {
  key: string;
}

type StoredSyncEvent = CustomEvent<StoredSyncDetail>;

function isStoredSyncEvent(event: Event): event is StoredSyncEvent {
  return event instanceof CustomEvent;
}

function useStored<T>(
  key: string,
  fallback: T,
  parse: (raw: string) => T | undefined,
  serialize: (value: T) => string,
): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(fallback);

  function readStored(): T | undefined {
    const raw = window.localStorage.getItem(NAMESPACE + key);
    return raw === null ? undefined : parse(raw);
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: `parse` is a module-level function per hook; re-reading on every render would fight the setter
  useEffect(() => {
    const stored = readStored();
    if (stored !== undefined) setValue(stored);
  }, [key]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `parse` is a module-level function per hook, same as the mount read above
  useEffect(() => {
    function onSync(event: Event): void {
      if (!isStoredSyncEvent(event) || event.detail.key !== key) return;
      const stored = readStored();
      if (stored !== undefined) setValue(stored);
    }
    window.addEventListener(SYNC_EVENT, onSync);
    return () => window.removeEventListener(SYNC_EVENT, onSync);
  }, [key]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `serialize` is a module-level function per hook
  const set = useCallback(
    (next: T) => {
      setValue(next);
      window.localStorage.setItem(NAMESPACE + key, serialize(next));
      window.dispatchEvent(new CustomEvent<StoredSyncDetail>(SYNC_EVENT, { detail: { key } }));
    },
    [key],
  );

  return [value, set];
}

const parseFlag = (raw: string): boolean | undefined =>
  raw === "1" ? true : raw === "0" ? false : undefined;
const serializeFlag = (value: boolean): string => (value ? "1" : "0");

export function useStoredFlag(key: string, fallback: boolean): [boolean, (next: boolean) => void] {
  return useStored(key, fallback, parseFlag, serializeFlag);
}

const parseNumber = (raw: string): number | undefined => {
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : undefined;
};

export function useStoredNumber(key: string, fallback: number): [number, (next: number) => void] {
  return useStored(key, fallback, parseNumber, String);
}

// A preference drawn from a closed set of names, stored as the plain name. Anything else on
// disk (an old name, a hand edit) reads as absent, so the fallback stands.
export function useStoredChoice<T extends string>(
  key: string,
  fallback: T,
  allowed: readonly T[],
): [T, (next: T) => void] {
  return useStored(
    key,
    fallback,
    (raw) => allowed.find((option) => option === raw),
    (value) => value,
  );
}

// A preference with structure -- a window's corner and size, say. `parse` owns what a valid
// shape is; broken json and a shape it rejects both read as absent.
export function useStoredJson<T>(
  key: string,
  fallback: T,
  parse: (value: unknown) => T | undefined,
): [T, (next: T) => void] {
  return useStored(
    key,
    fallback,
    (raw) => {
      try {
        const decoded: unknown = JSON.parse(raw);
        return parse(decoded);
      } catch {
        return undefined;
      }
    },
    (value) => JSON.stringify(value),
  );
}
