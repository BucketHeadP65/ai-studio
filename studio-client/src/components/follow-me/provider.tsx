"use client";

import { usePathname, useSearchParams } from "next/navigation";
import {
  type ReactElement,
  type ReactNode,
  Suspense,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
} from "react";

import type { PageContext, PageTrailStep, TargetDecl } from "@/api/page-context";

import { idsOf, pageOf } from "./page-map";

import type { Registered, TargetHandle } from "./targets";

// Ambient knowledge of where a person is and where they came from, so a chat turn can carry it
// without every caller wiring pathname/search params by hand. `FollowMeContext.Provider` wraps
// `children` unconditionally, so `useFollowMe()` never throws anywhere in this app's tree.
//
// `usePathname()`/`useSearchParams()` need their own Suspense boundary (Next's CSR-bailout rule
// for static rendering); `NavigationTracker` isolates them below the context provider but beside
// `children`, so a suspension there never hides the context or the app. Until it reports the
// first navigation, `collectContext()` reads an empty pathname and degrades to
// `{url: "", page: "unknown", ...}` rather than throwing.
//
// `collectContext()` is a snapshot, not a subscription: reading it empties the trail buffer, so
// the same step never rides two turns. `registerExtras` lets a page volunteer a few field values
// a wider audience wouldn't otherwise see (the unsaved title of a draft, say) -- one entry per
// registrant id, last write wins. `registerTarget` lets a page declare something a companion can
// point at, prefill, or switch: one entry per target name, keyed so a page unmounting always
// clears what it registered.

const TRAIL_LIMIT = 10;
// What the chat wire takes in one context. Registrations past this are dropped rather than
// sending a body the boundary rejects whole.
const TARGET_LIMIT = 30;
const EMPTY_SEARCH = new URLSearchParams();

function urlOf(pathname: string, search: URLSearchParams): string {
  return search.size > 0 ? `${pathname}?${search.toString()}` : pathname;
}

// The tab in view, read off the first registered tab group that can report one. Empty when no
// group is registered, none can say, or the one that answers is showing nothing named.
function tabOf(registrations: Iterable<Registered>): string {
  for (const registered of registrations) {
    if (registered.decl.kind !== "tab" || registered.handle.readTab === undefined) continue;
    try {
      const tab = registered.handle.readTab();
      if (tab !== "") return tab;
    } catch {
      // A misbehaving reader contributes nothing, and never breaks the collect.
    }
  }
  return "";
}

export interface FollowMe {
  collectContext: () => PageContext;
  registerExtras: (id: string, read: () => Record<string, string>) => () => void;
  registerTarget: (decl: TargetDecl, handle: TargetHandle) => () => void;
  findTarget: (name: string) => Registered | null;
  targetNames: () => string[];
  // Settles once the page at `url` is the page `collectContext` reports, or once `timeoutMs`
  // passes without it arriving. A route that never lands settles the wait all the same, so a
  // caller is never held on it forever.
  whenAt: (url: string, timeoutMs: number) => Promise<void>;
}

export const FollowMeContext = createContext<FollowMe | null>(null);

interface Navigated {
  pathname: string;
  search: URLSearchParams;
}

// One caller waiting for a route to land: the path it wants, and what to run when it does.
interface Waiting {
  path: string;
  arrived: () => void;
}

// Renders nothing; it only reports the current pathname/search up to the provider that mounts it.
function NavigationTracker({
  onNavigate,
}: {
  onNavigate: (pathname: string, search: URLSearchParams) => void;
}): null {
  const pathname = usePathname();
  const search = useSearchParams();

  useEffect(() => {
    onNavigate(pathname, search);
  }, [pathname, search, onNavigate]);

  return null;
}

export function FollowMeProvider({ children }: { children: ReactNode }): ReactElement {
  const navRef = useRef<Navigated | null>(null);
  const trailRef = useRef<PageTrailStep[]>([]);
  const extrasRef = useRef<Map<string, () => Record<string, string>>>(new Map());
  const targetsRef = useRef<Map<string, Registered>>(new Map());
  const waitingRef = useRef<Set<Waiting>>(new Set());

  const handleNavigate = useCallback((pathname: string, search: URLSearchParams) => {
    const previous = navRef.current;
    if (previous !== null) {
      const previousUrl = urlOf(previous.pathname, previous.search);
      const url = urlOf(pathname, search);
      if (previousUrl !== url) {
        trailRef.current.push({ url: previousUrl, at: new Date().toISOString() });
        if (trailRef.current.length > TRAIL_LIMIT) trailRef.current.shift();
      }
    }
    navRef.current = { pathname, search };
    for (const waiting of [...waitingRef.current]) {
      if (waiting.path !== pathname) continue;
      waitingRef.current.delete(waiting);
      waiting.arrived();
    }
  }, []);

  const whenAt = useCallback((url: string, timeoutMs: number): Promise<void> => {
    const path = url.split("?")[0] ?? "";
    if (navRef.current?.pathname === path) return Promise.resolve();
    return new Promise<void>((resolve) => {
      let ranOut = 0;
      const waiting: Waiting = {
        path,
        arrived: () => {
          window.clearTimeout(ranOut);
          resolve();
        },
      };
      ranOut = window.setTimeout(() => {
        waitingRef.current.delete(waiting);
        resolve();
      }, timeoutMs);
      waitingRef.current.add(waiting);
    });
  }, []);

  const collectContext = useCallback((): PageContext => {
    const nav = navRef.current;
    const pathname = nav?.pathname ?? "";
    const search = nav?.search ?? EMPTY_SEARCH;

    const extras: Record<string, string> = {};
    for (const read of extrasRef.current.values()) {
      try {
        Object.assign(extras, read());
      } catch {
        // A misbehaving reader contributes nothing; it never breaks the collect.
      }
    }
    const trail = [...trailRef.current];
    trailRef.current = [];

    const registrations = [...targetsRef.current.values()];

    return {
      url: urlOf(pathname, search),
      page: pageOf(pathname),
      ids: idsOf(pathname, search),
      tab: tabOf(registrations),
      trail,
      extras,
      targets: registrations.slice(0, TARGET_LIMIT).map((registered) => registered.decl),
    };
  }, []);

  const registerExtras = useCallback((id: string, read: () => Record<string, string>) => {
    extrasRef.current.set(id, read);
    return () => {
      extrasRef.current.delete(id);
    };
  }, []);

  const registerTarget = useCallback((decl: TargetDecl, handle: TargetHandle) => {
    const registered = { decl, handle };
    targetsRef.current.set(decl.name, registered);
    return () => {
      // Only if this registration is still the one under that name. Two pages can declare the same
      // target, and their mount and unmount overlap during a route change, so an unguarded delete
      // lets the one leaving take the arriving one's entry with it. The verb then answers that the
      // page has no such target while the person is looking straight at it.
      if (targetsRef.current.get(decl.name) === registered) {
        targetsRef.current.delete(decl.name);
      }
    };
  }, []);

  const findTarget = useCallback(
    (name: string): Registered | null => targetsRef.current.get(name) ?? null,
    [],
  );

  const targetNames = useCallback((): string[] => [...targetsRef.current.keys()], []);

  const value = useMemo<FollowMe>(
    () => ({ collectContext, registerExtras, registerTarget, findTarget, targetNames, whenAt }),
    [collectContext, registerExtras, registerTarget, findTarget, targetNames, whenAt],
  );

  return (
    <FollowMeContext.Provider value={value}>
      <Suspense fallback={null}>
        <NavigationTracker onNavigate={handleNavigate} />
      </Suspense>
      {children}
    </FollowMeContext.Provider>
  );
}

export function useFollowMe(): FollowMe {
  const value = useContext(FollowMeContext);
  if (value === null) {
    throw new Error("useFollowMe must be used within a FollowMeProvider");
  }
  return value;
}
