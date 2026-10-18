import { useRouter } from "next/navigation";
import { useCallback } from "react";

import { type Verb, isVerb } from "@/api/page-context";

import { pageOf } from "./page-map";
import { useFollowMe } from "./provider";

import type { Registered } from "./targets";

// What executeVerb needs from the page it runs on: the registry to look a target up in,
// a way to move to another route, and a way to draw attention to an element.
export interface VerbDeps {
  findTarget: (name: string) => Registered | null;
  targetNames: () => string[];
  // Moves to another route and settles once the page there is the one a page context would
  // report, or once the wait for it runs out.
  push: (url: string) => Promise<void>;
  highlight: (el: HTMLElement) => void;
}

const POINT_CLASS = "followme-pointed";
const POINT_MS = 1500;
// How long a navigate waits for the route it pushed before answering on what it has. The next
// instruction block is meant to describe the page the person was moved to, and a block that
// never arrives is worse than one describing the page they were on.
export const NAVIGATION_WAIT_MS = 1500;

// Scrolls a target into view and outlines it briefly so a viewer can find what the
// companion is talking about.
export function highlightElement(el: HTMLElement): void {
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.add(POINT_CLASS);
  window.setTimeout(() => el.classList.remove(POINT_CLASS), POINT_MS);
}

function field(input: unknown, name: string): string {
  if (typeof input !== "object" || input === null) return "";
  const value = (input as Record<string, unknown>)[name];
  return typeof value === "string" ? value : "";
}

function missing(target: string, deps: VerbDeps): string {
  const names = deps.targetNames();
  return `no such target here: ${target}. Targets on this page: ${names.length > 0 ? names.join(", ") : "none"}`;
}

// Runs one companion verb against the current page and reports what happened in a
// sentence the agent reads back. Every outcome is a plain string: there is nothing else
// for a caller to branch on. A navigate settles only once the page it asked for is the page
// on screen, so whoever reads the outcome reads the new page beside it.
export async function executeVerb(name: Verb, input: unknown, deps: VerbDeps): Promise<string> {
  if (name === "navigate") {
    const url = field(input, "url");
    const path = url.split("?")[0] ?? "";
    if (!url.startsWith("/") || pageOf(path) === "unknown")
      return `refused: ${url} is not a dashboard route`;
    await deps.push(url);
    return `navigated to ${url}`;
  }

  const target = field(input, "target");
  const found = deps.findTarget(target);
  if (found === null) return missing(target, deps);

  if (name === "point") {
    const el = found.handle.element?.() ?? null;
    if (el !== null) deps.highlight(el);
    return `pointed at ${target}`;
  }

  if (name === "prefill") {
    if (found.decl.kind !== "input" || found.handle.prefill === undefined)
      return `${target} is not an input`;
    const text = field(input, "text");
    found.handle.prefill(text);
    return `prefilled ${target} with ${text.length} characters`;
  }

  const tab = field(input, "tab");
  if (found.decl.kind !== "tab" || found.handle.setTab === undefined)
    return `${target} is not a tab group`;
  if (!found.handle.setTab(tab)) {
    return `no such tab ${tab} on ${target}, it has: ${found.decl.params?.tabs ?? "none"}`;
  }
  return `switched ${target} to ${tab}`;
}

// A stable callback a chat surface calls with a companion tool's name and input once its
// outcome is needed. Returns null, and only null, for anything that is not one of the four
// verbs, so a caller can tell a verb outcome apart from a console act it must handle
// differently. A page's own handler throwing, or the router refusing the navigation, resolves
// to a failure sentence rather than propagating: the agent always gets a string back.
export function useVerbExecutor(): (name: string, input: unknown) => Promise<string> | null {
  const router = useRouter();
  const { findTarget, targetNames, whenAt } = useFollowMe();

  return useCallback(
    (name: string, input: unknown) => {
      if (!isVerb(name)) return null;
      return executeVerb(name, input, {
        findTarget,
        targetNames,
        push: async (url) => {
          router.push(url);
          await whenAt(url, NAVIGATION_WAIT_MS);
        },
        highlight: highlightElement,
      }).catch(() => `failed to run ${name}`);
    },
    [router, findTarget, targetNames, whenAt],
  );
}
