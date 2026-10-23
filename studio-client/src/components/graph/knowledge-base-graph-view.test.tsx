import type { GraphResult } from "@/api/types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { KnowledgeBaseGraphView } from "./knowledge-base-graph-view";

// The graph mounts sigma on a real WebGL canvas, jsdom can't run that, so this
// is the one allowed last-resort vi.mock: the canvas is a DOM-less library
// boundary. The stub renders the node count so the test can still assert that
// fetched data flowed into the canvas; everything else (the fetch, the knowledge
// base scoping, the filter→refetch query params) is exercised for real via MSW.
vi.mock("./graph-canvas", () => ({
  GraphCanvas: ({ graph }: { graph: GraphResult }) => (
    <div data-testid="graph-canvas-stub">nodes:{graph.nodes.length}</div>
  ),
}));

afterEach(cleanup);

const GRAPH: GraphResult = {
  nodes: [
    { id: "p1", type: "paper", title: "P1", status: "synthesized", superseded: false },
    { id: "c1", type: "concept", title: "C1", status: "draft", superseded: false },
  ],
  edges: [{ src_id: "p1", dst_id: "c1", rel: "introduces", provenance_claim: null }],
};

// Records every /api/graph request's query params so the test can assert the
// knowledge_base param and that a filter change re-fetches with page_type / rel in
// the URL.
function installGraphHandler(): { calls: URLSearchParams[] } {
  const calls: URLSearchParams[] = [];
  server.use(
    http.get("http://api.test/api/graph", ({ request }) => {
      calls.push(new URL(request.url).searchParams);
      return HttpResponse.json(GRAPH);
    }),
  );
  return { calls };
}

describe("KnowledgeBaseGraphView", () => {
  it("scopes the fetch to the given knowledge base and loads its graph", async () => {
    const { calls } = installGraphHandler();
    render(<KnowledgeBaseGraphView knowledgeBaseName="research" />);
    await waitFor(() =>
      expect(screen.getByTestId("graph-canvas-stub")).toHaveTextContent("nodes:2"),
    );
    expect(calls[0].get("knowledge_base")).toBe("research");
  });

  it("re-fetches with page_type in the query string when the filter changes", async () => {
    const { calls } = installGraphHandler();
    render(<KnowledgeBaseGraphView knowledgeBaseName="research" />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas-stub")).toBeInTheDocument());

    await userEvent.selectOptions(screen.getByLabelText("Page type"), "paper");

    await waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    const last = calls[calls.length - 1];
    expect(last.get("knowledge_base")).toBe("research");
    expect(last.get("page_type")).toBe("paper");
    expect(last.has("rel")).toBe(false);
  });

  it("re-fetches with rel in the query string when the relation filter changes", async () => {
    const { calls } = installGraphHandler();
    render(<KnowledgeBaseGraphView knowledgeBaseName="research" />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas-stub")).toBeInTheDocument());

    await userEvent.selectOptions(screen.getByLabelText("Relation"), "introduces");

    await waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    expect(calls[calls.length - 1].get("rel")).toBe("introduces");
  });

  // 343 nodes is not navigable by dragging alone, and a canvas with no counts
  // and no key is a picture rather than a reading surface.
  it("says how much is on screen and keys the types present", async () => {
    installGraphHandler();
    render(<KnowledgeBaseGraphView knowledgeBaseName="research" />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas-stub")).toBeInTheDocument());
    expect(screen.getByText(/2 pages/)).toBeInTheDocument();
    expect(screen.getByText(/1 link/)).toBeInTheDocument();
    const key = screen.getByRole("list", { name: /key/i });
    expect(key).toHaveTextContent("paper");
    expect(key).toHaveTextContent("concept");
  });

  it("finds a page by title and selects it", async () => {
    installGraphHandler();
    render(<KnowledgeBaseGraphView knowledgeBaseName="research" />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas-stub")).toBeInTheDocument());

    await userEvent.type(screen.getByRole("combobox", { name: /find a page/i }), "C1");
    await userEvent.click(await screen.findByRole("option", { name: /C1/ }));

    expect(screen.getByRole("complementary", { name: "Selection detail" })).toHaveTextContent("C1");
  });

  // A search box you can only reach with a mouse is half a search box.
  it("walks the suggestions with the arrow keys and picks with Enter", async () => {
    installGraphHandler();
    render(<KnowledgeBaseGraphView knowledgeBaseName="research" />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas-stub")).toBeInTheDocument());

    const box = screen.getByRole("combobox", { name: /find a page/i });
    await userEvent.type(box, "1");
    await userEvent.keyboard("{ArrowDown}{Enter}");

    expect(screen.getByRole("complementary", { name: "Selection detail" })).toHaveTextContent("P1");
    expect(box).toHaveValue("");
  });

  it("dismisses the suggestions on Escape without selecting anything", async () => {
    installGraphHandler();
    render(<KnowledgeBaseGraphView knowledgeBaseName="research" />);
    await waitFor(() => expect(screen.getByTestId("graph-canvas-stub")).toBeInTheDocument());

    await userEvent.type(screen.getByRole("combobox", { name: /find a page/i }), "1");
    // Scoped to the suggestion list: the two filter <select>s publish `option`
    // roles of their own, so an unscoped query is never empty.
    const list = () => screen.queryByRole("listbox", { name: "Matching pages" });
    expect(within(list() as HTMLElement).getAllByRole("option").length).toBeGreaterThan(0);
    await userEvent.keyboard("{Escape}");

    expect(list()).not.toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Selection detail" })).toHaveTextContent(
      /select a node or edge/i,
    );
  });

  it("surfaces a fetch error", async () => {
    server.use(
      http.get("http://api.test/api/graph", () => HttpResponse.text("boom", { status: 500 })),
    );
    render(<KnowledgeBaseGraphView knowledgeBaseName="research" />);
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/failed to load graph/i),
    );
  });
});
