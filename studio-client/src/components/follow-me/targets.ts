import { useContext, useEffect, useRef } from "react";

import type { TargetDecl } from "@/api/page-context";

import { FollowMeContext } from "./provider";

// What a page hands the registry for one named target: a way to find its element for
// scrolling and highlighting, and, for the kinds that support it, a way to act on it.
export interface TargetHandle {
  element?: () => HTMLElement | null;
  prefill?: (text: string) => void;
  setTab?: (tab: string) => boolean;
  // Which tab this group is showing now, for a tab target that can say. What the page context
  // reports as the tab in view, so a switch is visible to whoever asked for it.
  readTab?: () => string;
}

export interface Registered {
  decl: TargetDecl;
  handle: TargetHandle;
}

// Registers the target for as long as the component stays mounted, keeping its handle
// current across renders without re-registering on every one. A null declaration means
// the target does not exist right now: nothing is registered, and any earlier
// registration this call held is removed. Outside a FollowMeProvider it does nothing.
export function useTargetRegistration(decl: TargetDecl | null, handle: TargetHandle): void {
  const followMe = useContext(FollowMeContext);
  const handleRef = useRef(handle);
  handleRef.current = handle;
  const key = decl === null ? null : JSON.stringify(decl);

  useEffect(() => {
    if (followMe === null || key === null) return undefined;
    const wrapped: TargetHandle = { element: () => handleRef.current.element?.() ?? null };
    if (handleRef.current.prefill !== undefined) {
      wrapped.prefill = (text) => handleRef.current.prefill?.(text);
    }
    if (handleRef.current.setTab !== undefined) {
      wrapped.setTab = (tab) => handleRef.current.setTab?.(tab) ?? false;
    }
    if (handleRef.current.readTab !== undefined) {
      wrapped.readTab = () => handleRef.current.readTab?.() ?? "";
    }
    return followMe.registerTarget(JSON.parse(key) as TargetDecl, wrapped);
  }, [followMe, key]);
}
