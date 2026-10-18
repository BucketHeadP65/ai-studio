import { describe, expect, it, vi } from "vitest";

import { render } from "@/test-support/render";

// The mechanism: a probe reads `useFollowMe()` on every render and this file drives navigation by
// reassigning the mocked `next/navigation` values and re-rendering, the way `chat/page.test.tsx`
// does for its own router mocks.

let pathnameValue = "/chat";
let searchParamsValue = new URLSearchParams();

vi.mock("next/navigation", () => ({
  usePathname: () => pathnameValue,
  useSearchParams: () => searchParamsValue,
}));

// Imported after the mock so the components under test see the mocked module.
import type { FollowMe } from "./provider";
import { FollowMeProvider, useFollowMe } from "./provider";

let followMe: FollowMe = {
  collectContext: () => {
    throw new Error("Probe did not mount");
  },
  registerExtras: () => () => {},
  registerTarget: () => () => {},
  findTarget: () => null,
  targetNames: () => [],
  whenAt: () => Promise.resolve(),
};

function Probe(): null {
  followMe = useFollowMe();
  return null;
}

function renderProbe() {
  return render(
    <FollowMeProvider>
      <Probe />
    </FollowMeProvider>,
  );
}

describe("FollowMeProvider", () => {
  it("names the current page and its url", () => {
    pathnameValue = "/workflows/library/wf.digest.v3";
    searchParamsValue = new URLSearchParams();
    renderProbe();

    const context = followMe.collectContext();

    expect(context.page).toBe("workflow-about");
    expect(context.url).toBe("/workflows/library/wf.digest.v3");
    expect(context.ids).toEqual({ workflow: "wf.digest.v3" });
    expect(context.tab).toBe("");
  });

  it("joins the query onto the url when one is present", () => {
    pathnameValue = "/chat";
    searchParamsValue = new URLSearchParams("session=s-2");
    renderProbe();

    expect(followMe.collectContext().url).toBe("/chat?session=s-2");
  });

  it("carries the prior url in the trail after navigating, then clears it on the next collect", () => {
    pathnameValue = "/chat";
    searchParamsValue = new URLSearchParams();
    const { rerender } = renderProbe();

    pathnameValue = "/projects";
    rerender(
      <FollowMeProvider>
        <Probe />
      </FollowMeProvider>,
    );

    const first = followMe.collectContext();
    expect(first.trail).toEqual([{ url: "/chat", at: expect.any(String) }]);

    const second = followMe.collectContext();
    expect(second.trail).toEqual([]);
  });

  it("caps the trail at 10 entries, dropping the oldest first", () => {
    pathnameValue = "/page-0";
    searchParamsValue = new URLSearchParams();
    const { rerender } = renderProbe();

    for (let step = 1; step <= 12; step += 1) {
      pathnameValue = `/page-${step}`;
      rerender(
        <FollowMeProvider>
          <Probe />
        </FollowMeProvider>,
      );
    }

    const { trail } = followMe.collectContext();

    expect(trail).toHaveLength(10);
    expect(trail.map((step) => step.url)).toEqual([
      "/page-2",
      "/page-3",
      "/page-4",
      "/page-5",
      "/page-6",
      "/page-7",
      "/page-8",
      "/page-9",
      "/page-10",
      "/page-11",
    ]);
  });

  it("merges registered extras into the collected context and forgets them once unregistered", () => {
    pathnameValue = "/workflows/draft";
    searchParamsValue = new URLSearchParams();
    renderProbe();

    const unregister = followMe.registerExtras("draft", () => ({ draft: "x" }));
    expect(followMe.collectContext().extras).toEqual({ draft: "x" });

    unregister();
    expect(followMe.collectContext().extras).toEqual({});
  });

  it("a reader that throws contributes nothing and does not break the collect", () => {
    pathnameValue = "/chat";
    searchParamsValue = new URLSearchParams();
    renderProbe();

    followMe.registerExtras("bad", () => {
      throw new Error("boom");
    });
    followMe.registerExtras("good", () => ({ good: "1" }));

    expect(followMe.collectContext().extras).toEqual({ good: "1" });
  });

  it("registers a target, lists it in the context, and forgets it on unregister", () => {
    pathnameValue = "/chat";
    searchParamsValue = new URLSearchParams();
    renderProbe();

    const el = document.createElement("div");
    const unregister = followMe.registerTarget(
      {
        name: "knowledge-base-tabs",
        kind: "tab",
        description: "Overview or Graph",
        params: { tabs: "overview, graph" },
      },
      { element: () => el, setTab: () => true },
    );

    expect(followMe.collectContext().targets).toEqual([
      {
        name: "knowledge-base-tabs",
        kind: "tab",
        description: "Overview or Graph",
        params: { tabs: "overview, graph" },
      },
    ]);
    expect(followMe.findTarget("knowledge-base-tabs")?.handle.element?.()).toBe(el);

    unregister();

    expect(followMe.collectContext().targets).toEqual([]);
    expect(followMe.findTarget("knowledge-base-tabs")).toBeNull();
  });

  it("lets a replacing registration survive the replaced one's unmount", () => {
    // Two pages declare the same target and their lifecycles overlap on a route change: the arriving
    // one registers before the leaving one cleans up. An unguarded cleanup would take the survivor's
    // entry with it, and the verb would answer that the page has no such target while it is on screen.
    pathnameValue = "/knowledge-bases/research";
    searchParamsValue = new URLSearchParams();
    renderProbe();

    const leaving = document.createElement("div");
    const arriving = document.createElement("div");
    const decl = { name: "knowledge-base-tabs", kind: "tab", description: "Overview or Graph" } as const;

    const unregisterLeaving = followMe.registerTarget(decl, {
      element: () => leaving,
      setTab: () => true,
    });
    followMe.registerTarget(decl, { element: () => arriving, setTab: () => true });

    unregisterLeaving();

    expect(followMe.findTarget("knowledge-base-tabs")?.handle.element?.()).toBe(arriving);
    expect(followMe.collectContext().targets).toHaveLength(1);
  });

  it("reports the tab a registered tab group is showing, live", () => {
    pathnameValue = "/knowledge-bases/research";
    searchParamsValue = new URLSearchParams();
    renderProbe();

    let showing = "overview";
    followMe.registerTarget(
      { name: "knowledge-base-tabs", kind: "tab", description: "Overview or Graph" },
      { readTab: () => showing, setTab: () => true },
    );

    expect(followMe.collectContext().tab).toBe("overview");

    showing = "graph";
    expect(followMe.collectContext().tab).toBe("graph");
  });

  it("leaves the tab empty when no registered group can say which tab is showing", () => {
    pathnameValue = "/knowledge-bases/research";
    searchParamsValue = new URLSearchParams();
    renderProbe();

    followMe.registerTarget(
      { name: "knowledge-base-tabs", kind: "tab", description: "Overview or Graph" },
      { setTab: () => true },
    );
    followMe.registerTarget(
      { name: "broken-tabs", kind: "tab", description: "" },
      {
        readTab: () => {
          throw new Error("boom");
        },
      },
    );

    expect(followMe.collectContext().tab).toBe("");
  });

  it("caps the reported targets at the 30 the wire takes, dropping the excess", () => {
    pathnameValue = "/chat";
    searchParamsValue = new URLSearchParams();
    renderProbe();

    for (let index = 0; index < 31; index += 1) {
      followMe.registerTarget({ name: `t-${index}`, kind: "region", description: "" }, {});
    }

    const { targets } = followMe.collectContext();

    expect(targets).toHaveLength(30);
    expect(targets.at(-1)?.name).toBe("t-29");
  });
});
