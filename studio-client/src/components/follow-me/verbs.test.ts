import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const pathnameValue = "/chat";
const searchParamsValue = new URLSearchParams();
const routerPush = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => pathnameValue,
  useSearchParams: () => searchParamsValue,
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the hooks under test see the mocked module.
import { FollowMeProvider, useFollowMe } from "./provider";
import { executeVerb, useVerbExecutor } from "./verbs";

function deps(overrides: Partial<Parameters<typeof executeVerb>[2]> = {}) {
  const el = document.createElement("div");
  const prefill = vi.fn();
  const setTab = vi.fn((tab: string) => tab === "graph");
  return {
    el,
    prefill,
    setTab,
    push: vi.fn(async () => {}),
    highlight: vi.fn(),
    targetNames: () => ["knowledge-base-tabs", "composer"],
    findTarget: (name: string) =>
      name === "knowledge-base-tabs"
        ? {
            decl: {
              name,
              kind: "tab" as const,
              description: "",
              params: { tabs: "overview, graph" },
            },
            handle: { element: () => el, setTab },
          }
        : name === "composer"
          ? {
              decl: { name, kind: "input" as const, description: "" },
              handle: { element: () => el, prefill },
            }
          : null,
    ...overrides,
  };
}

describe("executeVerb", () => {
  it("navigates to a dashboard route and refuses anything else", async () => {
    const d = deps();
    await expect(executeVerb("navigate", { url: "/knowledge-bases/research" }, d)).resolves.toBe(
      "navigated to /knowledge-bases/research",
    );
    expect(d.push).toHaveBeenCalledWith("/knowledge-bases/research");
    await expect(executeVerb("navigate", { url: "/" }, d)).resolves.toBe("navigated to /");
    expect(d.push).toHaveBeenCalledWith("/");
    await expect(executeVerb("navigate", { url: "https://evil.example" }, d)).resolves.toBe(
      "refused: https://evil.example is not a dashboard route",
    );
    await expect(executeVerb("navigate", { url: "/nowhere" }, d)).resolves.toBe(
      "refused: /nowhere is not a dashboard route",
    );
  });

  it("answers on what it has when the page it pushed never arrives", async () => {
    const d = deps({ push: vi.fn(async () => {}) });

    await expect(executeVerb("navigate", { url: "/knowledge-bases/research" }, d)).resolves.toBe(
      "navigated to /knowledge-bases/research",
    );
  });

  it("points at a registered target and names the targets when it is missing", async () => {
    const d = deps();
    await expect(executeVerb("point", { target: "composer" }, d)).resolves.toBe(
      "pointed at composer",
    );
    expect(d.highlight).toHaveBeenCalledWith(d.el);
    await expect(executeVerb("point", { target: "ghost" }, d)).resolves.toBe(
      "no such target here: ghost. Targets on this page: knowledge-base-tabs, composer",
    );
  });

  it("prefills an input and counts the characters", async () => {
    const d = deps();
    await expect(
      executeVerb("prefill", { target: "composer", text: "hello there" }, d),
    ).resolves.toBe("prefilled composer with 11 characters");
    expect(d.prefill).toHaveBeenCalledWith("hello there");
    await expect(executeVerb("prefill", { target: "knowledge-base-tabs", text: "x" }, d)).resolves.toBe(
      "knowledge-base-tabs is not an input",
    );
  });

  it("switches a tab group and names its tabs on a miss", async () => {
    const d = deps();
    await expect(executeVerb("set_tab", { target: "knowledge-base-tabs", tab: "graph" }, d)).resolves.toBe(
      "switched knowledge-base-tabs to graph",
    );
    await expect(executeVerb("set_tab", { target: "knowledge-base-tabs", tab: "nope" }, d)).resolves.toBe(
      "no such tab nope on knowledge-base-tabs, it has: overview, graph",
    );
  });

  it("says none when no targets are registered", async () => {
    await expect(
      executeVerb(
        "point",
        { target: "x" },
        deps({ targetNames: () => [], findTarget: () => null }),
      ),
    ).resolves.toBe("no such target here: x. Targets on this page: none");
  });
});

describe("useVerbExecutor", () => {
  it("reports a page handler's throw as a failure instead of letting it propagate", async () => {
    const { result } = renderHook(() => ({ followMe: useFollowMe(), run: useVerbExecutor() }), {
      wrapper: FollowMeProvider,
    });

    result.current.followMe.registerTarget(
      { name: "composer", kind: "input", description: "" },
      {
        element: () => document.createElement("div"),
        prefill: () => {
          throw new Error("boom");
        },
      },
    );

    await expect(result.current.run("prefill", { target: "composer", text: "hi" })).resolves.toBe(
      "failed to run prefill",
    );
  });

  it("reports the router throwing as a failure too", async () => {
    routerPush.mockImplementationOnce(() => {
      throw new Error("boom");
    });
    const { result } = renderHook(() => useVerbExecutor(), { wrapper: FollowMeProvider });

    await expect(result.current("navigate", { url: "/chat" })).resolves.toBe(
      "failed to run navigate",
    );
  });
});
