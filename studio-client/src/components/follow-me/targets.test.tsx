import { renderHook } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import type { TargetDecl } from "@/api/page-context";

import type { FollowMe } from "./provider";
import { FollowMeContext } from "./provider";
import { useTargetRegistration } from "./targets";
import type { Registered } from "./targets";
import { executeVerb } from "./verbs";

// A hand-built FollowMe rather than the real provider, so registerTarget's call count is
// directly observable without also standing up next/navigation.
function fakeFollowMe(): { followMe: FollowMe; registerCalls: () => number } {
  const targets = new Map<string, Registered>();
  let calls = 0;
  const followMe: FollowMe = {
    collectContext: () => {
      throw new Error("not used in this test");
    },
    registerExtras: () => () => {},
    registerTarget: (decl, handle) => {
      calls += 1;
      targets.set(decl.name, { decl, handle });
      return () => {
        targets.delete(decl.name);
      };
    },
    findTarget: (name) => targets.get(name) ?? null,
    targetNames: () => [...targets.keys()],
    whenAt: () => Promise.resolve(),
  };
  return { followMe, registerCalls: () => calls };
}

function wrapperFor(followMe: FollowMe) {
  return function Wrapper({ children }: { children: ReactNode }): ReactElement {
    return <FollowMeContext.Provider value={followMe}>{children}</FollowMeContext.Provider>;
  };
}

describe("useTargetRegistration", () => {
  it("registers nothing for a null declaration", () => {
    const { followMe, registerCalls } = fakeFollowMe();

    renderHook(
      () => useTargetRegistration(null, { element: () => document.createElement("div") }),
      {
        wrapper: wrapperFor(followMe),
      },
    );

    expect(registerCalls()).toBe(0);
    expect(followMe.targetNames()).toEqual([]);
  });

  it("unregisters when a declaration transitions from present to null", () => {
    const { followMe } = fakeFollowMe();
    let decl: TargetDecl | null = { name: "knowledge-base-tabs", kind: "tab", description: "d" };

    const { rerender } = renderHook(
      () => useTargetRegistration(decl, { element: () => document.createElement("div") }),
      { wrapper: wrapperFor(followMe) },
    );

    expect(followMe.targetNames()).toEqual(["knowledge-base-tabs"]);

    decl = null;
    rerender();

    expect(followMe.targetNames()).toEqual([]);
  });

  it("does not re-register on a rerender carrying an equal but new declaration object", () => {
    const { followMe, registerCalls } = fakeFollowMe();
    const handle = { element: () => document.createElement("div") };

    const { rerender } = renderHook(
      () => useTargetRegistration({ name: "knowledge-base-tabs", kind: "tab", description: "d" }, handle),
      { wrapper: wrapperFor(followMe) },
    );

    rerender();
    rerender();

    expect(registerCalls()).toBe(1);
    expect(followMe.targetNames()).toEqual(["knowledge-base-tabs"]);
  });

  it("removes its target on unmount so it cannot leak into the next page's context", () => {
    const { followMe } = fakeFollowMe();

    const { unmount } = renderHook(
      () =>
        useTargetRegistration(
          { name: "knowledge-base-tabs", kind: "tab", description: "d" },
          { element: () => document.createElement("div") },
        ),
      { wrapper: wrapperFor(followMe) },
    );

    expect(followMe.targetNames()).toEqual(["knowledge-base-tabs"]);

    unmount();

    expect(followMe.targetNames()).toEqual([]);
  });

  it("honors a handle that omits prefill: an input target refuses instead of faking success", async () => {
    const { followMe } = fakeFollowMe();

    renderHook(
      () =>
        useTargetRegistration(
          { name: "composer", kind: "input", description: "" },
          { element: () => document.createElement("div") },
        ),
      { wrapper: wrapperFor(followMe) },
    );

    const outcome = await executeVerb(
      "prefill",
      { target: "composer", text: "hello" },
      {
        findTarget: followMe.findTarget,
        targetNames: followMe.targetNames,
        push: vi.fn(async () => {}),
        highlight: vi.fn(),
      },
    );

    expect(outcome).toBe("composer is not an input");
  });
});
