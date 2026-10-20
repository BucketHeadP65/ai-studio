import { act, cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { KnowledgeBaseDetail } from "@/api/types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { KnowledgeBaseDetailView } from "./detail";

// The Graph tab mounts KnowledgeBaseGraphView, which dynamic-imports GraphCanvas — sigma
// reads WebGL at module evaluation, which jsdom lacks, so this is the one allowed
// last-resort vi.mock: the canvas is a DOM-less library boundary. It intercepts the
// same module KnowledgeBaseGraphView resolves via its relative import, since Vitest mocks
// key off the resolved module, not the import specifier.
vi.mock("@/components/graph/graph-canvas", () => ({
  GraphCanvas: () => <div data-testid="graph-canvas-stub" />,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/knowledge-bases/tube",
  useSearchParams: () => new URLSearchParams(),
}));

// Imported after the mock so the components under test see the mocked module.
import type { FollowMe } from "@/components/follow-me/provider";
import { FollowMeProvider, useFollowMe } from "@/components/follow-me/provider";

afterEach(cleanup);

// RefreshCard and DecisionsCard (both mounted for every knowledge base) poll these on render —
// a plain empty answer here keeps those fetches out of every other test's way.
beforeEach(() => {
  server.use(
    http.get("http://api.test/api/knowledge-bases/:name/refresh-runs", () => HttpResponse.json({ refresh_runs: [] })),
    http.get("http://api.test/api/knowledge-bases/:name/decisions", () =>
      HttpResponse.json({ rows: [], skipped: [], outcomes_by_page_type: {} }),
    ),
  );
});

function detail(overrides: Partial<KnowledgeBaseDetail> = {}): KnowledgeBaseDetail {
  return {
    name: "tube",
    registered: true,
    indexed: true,
    broken: false,
    path: "/knowledge-bases/tube",
    description: "talks",
    pages: 3,
    page_types: ["talk"],
    last_indexed: "2026-08-20T09:00:00+00:00",
    sources_connectors: ["intake"],
    watch_list: [],
    validation: [],
    source_queue: [],
    last_refresh_run: null,
    schedule: null,
    ...overrides,
  };
}

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

function renderDetailWithProbe() {
  const rendered = render(
    <FollowMeProvider>
      <Probe />
      <KnowledgeBaseDetailView
        detail={detail()}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />
    </FollowMeProvider>,
  );
  return { ...rendered, followMe };
}

describe("KnowledgeBaseDetailView", () => {
  it("shows identity, vitals, and both status badges", () => {
    render(
      <KnowledgeBaseDetailView
        detail={detail()}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(screen.getByRole("heading", { name: "tube" })).toBeInTheDocument();
    expect(screen.getByText("registered")).toBeInTheDocument();
    expect(screen.getByText("indexed")).toBeInTheDocument();
    expect(screen.getByText(/\/knowledge-bases\/tube, talks/)).toBeInTheDocument();
    expect(screen.getByText("talk")).toBeInTheDocument();
  });

  it("submits a trimmed URL and clears the input", async () => {
    const onAddUrl = vi.fn();
    render(
      <KnowledgeBaseDetailView
        detail={detail()}
        onAddUrl={onAddUrl}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    const input = screen.getByRole("textbox", { name: "Source URL" });
    await userEvent.type(input, "  https://youtu.be/abc123def45  ");
    await userEvent.click(screen.getByRole("button", { name: "Add source" }));

    expect(onAddUrl).toHaveBeenCalledWith("https://youtu.be/abc123def45");
    expect(input).toHaveValue("");
  });

  it("hides the source queue section for a knowledge base without the intake connector", () => {
    render(
      <KnowledgeBaseDetailView
        detail={detail({ sources_connectors: [] })}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(screen.queryByRole("textbox", { name: "Source URL" })).not.toBeInTheDocument();
  });

  it("shows the source queue section for a knowledge base declaring the intake connector", () => {
    render(
      <KnowledgeBaseDetailView
        detail={detail({ sources_connectors: ["intake"] })}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Source URL" })).toBeInTheDocument();
  });

  it("hides the source queue section for a knowledge base declaring only the youtube connector", () => {
    render(
      <KnowledgeBaseDetailView
        detail={detail({ sources_connectors: ["youtube"] })}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    // The API refuses `POST .../source-queue` for it, so the form would be a dead one.
    expect(screen.queryByRole("textbox", { name: "Source URL" })).not.toBeInTheDocument();
  });

  it("hides the source queue section for a knowledge base declaring an unrelated connector", () => {
    render(
      <KnowledgeBaseDetailView
        detail={detail({ sources_connectors: ["zotero"] })}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(screen.queryByRole("textbox", { name: "Source URL" })).not.toBeInTheDocument();
  });

  it("lists source-queue rows with their kind and status, and failures carry the reason", () => {
    render(
      <KnowledgeBaseDetailView
        detail={detail({
          source_queue: [
            {
              key: "abc123def45",
              url: "https://youtu.be/abc123def45",
              source_type: "video",
              status: "resolved",
              reason: null,
              found_via: null,
              found_at: null,
            },
            {
              key: "zzz999zzz99",
              url: "https://youtu.be/zzz999zzz99",
              source_type: "video",
              status: "failed",
              reason: "no captions",
              found_via: null,
              found_at: null,
            },
          ],
        })}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(screen.getByText("abc123def45")).toBeInTheDocument();
    expect(screen.getAllByText("video")).toHaveLength(2);
    expect(screen.getByText("resolved")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("no captions")).toBeInTheDocument();
  });

  it("mounts the decisions card and lists the watch list", async () => {
    server.use(
      http.get("http://api.test/api/knowledge-bases/tube/decisions", () =>
        HttpResponse.json({
          rows: [
            {
              id: "flag.try-planning-reminders",
              page_type: "flag",
              title: "Try planning reminders",
              summary: "A live task list re-injected every turn.",
              status: "pending",
              links: { about: ["talk.agents-that-plan"] },
            },
          ],
          skipped: [],
          outcomes_by_page_type: {},
        }),
      ),
    );
    render(
      <KnowledgeBaseDetailView
        detail={detail({
          watch_list: [{ scanner: "xml", ref: "https://www.youtube.com/feeds/videos.xml?channel_id=UCabc123", max_items: 15, last_scanned: null }],
        })}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(screen.getByRole("heading", { name: "Decisions" })).toBeInTheDocument();
    expect(await screen.findByText("Try planning reminders")).toBeInTheDocument();
    expect(screen.getByText("https://www.youtube.com/feeds/videos.xml?channel_id=UCabc123")).toBeInTheDocument();
    expect(screen.getByText("never scanned")).toBeInTheDocument();
  });

  it("names the decision pages that failed to parse", async () => {
    server.use(
      http.get("http://api.test/api/knowledge-bases/tube/decisions", () =>
        HttpResponse.json({ rows: [], skipped: ["a.md", "b.md"], outcomes_by_page_type: {} }),
      ),
    );
    render(
      <KnowledgeBaseDetailView
        detail={detail()}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(await screen.findByText("2 pages could not be read (a.md, b.md).")).toBeInTheDocument();
  });

  it("renders standing validation rows by severity, and a clean index says so", () => {
    const { unmount } = render(
      <KnowledgeBaseDetailView
        detail={detail({
          validation: [
            {
              page_id: "talk.a",
              severity: "error",
              rule: "dangling-link",
              message: "unresolved link",
              anchor: "c2",
            },
            {
              page_id: "talk.b",
              severity: "warning",
              rule: "schema-vocabulary-drift",
              message: "seeded but missing",
              anchor: null,
            },
          ],
        })}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(screen.getByText("Standing validation, 2")).toBeInTheDocument();
    expect(screen.getByText("talk.a ^c2")).toBeInTheDocument();
    expect(screen.getByText("error")).toBeInTheDocument();
    expect(screen.getByText("warning")).toBeInTheDocument();
    unmount();

    render(
      <KnowledgeBaseDetailView
        detail={detail()}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );
    expect(screen.getByText("The last index is clean.")).toBeInTheDocument();
  });

  it("surfaces an add failure as an alert", () => {
    render(
      <KnowledgeBaseDetailView
        detail={detail()}
        onAddUrl={vi.fn()}
        adding={false}
        addError="not a video URL"
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("not a video URL");
  });
});

describe("KnowledgeBaseDetailView registration", () => {
  it("offers Unregister on a registered knowledge base", async () => {
    const onUnregister = vi.fn();
    render(
      <KnowledgeBaseDetailView
        detail={detail()}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={onUnregister}
        registering={false}
        registerError={null}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Unregister" }));
    expect(onUnregister).toHaveBeenCalled();
  });

  it("offers a path field and Register on an unregistered knowledge base", async () => {
    const onRegister = vi.fn();
    render(
      <KnowledgeBaseDetailView
        detail={detail({ registered: false, path: null })}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={onRegister}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    await userEvent.type(screen.getByRole("textbox", { name: "Knowledge base directory" }), "/kb/tube");
    await userEvent.click(screen.getByRole("button", { name: "Register" }));
    expect(onRegister).toHaveBeenCalledWith("/kb/tube");
  });
});

describe("KnowledgeBaseDetailView tabs", () => {
  it("shows the Overview panel by default", () => {
    render(
      <KnowledgeBaseDetailView
        detail={detail()}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    expect(screen.getByRole("tab", { name: "Overview", selected: true })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Registration" })).toBeInTheDocument();
  });

  it("switches to the Graph tab, hiding Overview, and fetches this knowledge base's own graph", async () => {
    let seenUrl = "";
    server.use(
      http.get("http://api.test/api/graph", ({ request }) => {
        seenUrl = request.url;
        return HttpResponse.json({ nodes: [], edges: [] });
      }),
    );
    render(
      <KnowledgeBaseDetailView
        detail={detail()}
        onAddUrl={vi.fn()}
        adding={false}
        addError={null}
        onRegister={vi.fn()}
        onUnregister={vi.fn()}
        registering={false}
        registerError={null}
      />,
    );

    await userEvent.click(screen.getByRole("tab", { name: "Graph" }));

    expect(screen.queryByRole("heading", { name: "Registration" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("graph-canvas-stub")).toBeInTheDocument());
    expect(new URL(seenUrl).searchParams.get("knowledge_base")).toBe("tube");
  });

  it("registers its tab group and switches tabs through the registry", () => {
    const { followMe } = renderDetailWithProbe();

    expect(followMe.collectContext().targets).toEqual([
      {
        name: "knowledge-base-tabs",
        kind: "tab",
        description: "Overview or Graph",
        params: { tabs: "overview, graph" },
      },
    ]);

    act(() => {
      expect(followMe.findTarget("knowledge-base-tabs")?.handle.setTab?.("graph")).toBe(true);
    });
    expect(screen.getByRole("tab", { name: "Graph" })).toHaveAttribute("aria-selected", "true");

    expect(followMe.findTarget("knowledge-base-tabs")?.handle.setTab?.("nope")).toBe(false);
  });

  it("reports the tab it is showing in the page context, before and after a switch", () => {
    const { followMe } = renderDetailWithProbe();

    expect(followMe.collectContext().tab).toBe("overview");

    act(() => {
      followMe.findTarget("knowledge-base-tabs")?.handle.setTab?.("graph");
    });

    expect(followMe.collectContext().tab).toBe("graph");
  });
});
