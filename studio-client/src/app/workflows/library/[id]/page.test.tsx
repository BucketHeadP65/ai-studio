import { screen, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { GraphNode, WorkflowDetail } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "wf.research-digest.v1" }),
  useRouter: () => ({ push: vi.fn() }),
}));

// Imported after the mock so the page sees the mocked module.
import WorkflowLibraryDetailPage from "./page";

const BASE = "http://studio.test";

function node(overrides: Partial<GraphNode> & Pick<GraphNode, "id" | "kind">): GraphNode {
  return {
    key: overrides.id,
    signature: "",
    required: true,
    detail: "",
    tools: [],
    is_output: false,
    unknown: false,
    brought: false,
    briefing: "",
    input_schemas: {},
    output_schema: null,
    source: "",
    ...overrides,
  };
}

// The real research digest workflow's own graph, `references` added parallel to `digests` so the
// side by side level names two nodes; the three output schemas are verbatim from the artifact.
const UNDIGESTED_LIST = {
  type: "array" as const,
  items: {
    type: "object" as const,
    title: "Undigested",
    required: ["source_id", "item_type", "title"],
    properties: {
      title: { type: "string" },
      item_type: { enum: ["video", "paper", "article", "post"], type: "string" },
      source_id: { type: "string" },
    },
  },
};

const DIGEST_RESULT = {
  type: "object" as const,
  title: "DigestResult",
  required: ["digest_id", "status"],
  properties: {
    reason: { type: "string" },
    status: { enum: ["written", "skipped"], type: "string" },
    digest_id: { type: "string" },
  },
};

const DIGEST_REPORT = {
  type: "object" as const,
  title: "DigestReport",
  required: ["written", "skipped", "requested"],
  properties: {
    skipped: { type: "integer" },
    written: { type: "integer" },
    requested: { type: "integer" },
  },
};

const DETAIL: WorkflowDetail = {
  ok: true,
  id: "wf.research-digest.v1",
  name: "research-digest",
  version: 1,
  description: "Digests every undigested source into the field's own notes.",
  title: "Research digest",
  summary: "Turns undigested sources into written notes and references.",
  applicability: "",
  provisional: false,
  graph: {
    name: "research-digest",
    version: 1,
    nodes: [
      node({
        key: "param:collection",
        id: "collection",
        kind: "param",
        signature: "string",
        detail: "the knowledge base to draw from",
      }),
      node({
        id: "undigested",
        kind: "run",
        detail: "Runs 'list_undigested' once, feeding the collection.",
        work: "list_undigested",
        output_schema: UNDIGESTED_LIST,
      }),
      node({
        id: "digests",
        kind: "map",
        detail: "Runs 'digest_source' once per item of undigested.",
        brought: true,
        work: "digest_source",
        input_schemas: { source: UNDIGESTED_LIST.items },
        output_schema: { type: "array", items: DIGEST_RESULT },
      }),
      node({
        id: "references",
        kind: "map",
        detail: "Runs 'find_references' once per item of undigested.",
        work: "find_references",
        input_schemas: { source: UNDIGESTED_LIST.items },
        output_schema: { type: "array", items: { type: "object", title: "Reference" } },
      }),
      node({
        id: "report",
        kind: "run",
        detail: "Runs 'compose_report' once, feeding digests and references.",
        is_output: true,
        work: "compose_report",
        input_schemas: {
          digests: { type: "array", items: DIGEST_RESULT },
          references: { type: "array", items: { type: "object", title: "Reference" } },
        },
        output_schema: DIGEST_REPORT,
      }),
    ],
    edges: [
      { from: "param:collection", to: "undigested", kind: "wire", label: "collection: string" },
      { from: "undigested", to: "digests", kind: "map", label: "each source: DigestResult" },
      { from: "undigested", to: "references", kind: "map", label: "each source: Reference" },
      { from: "digests", to: "report", kind: "wire", label: "digests: list[DigestResult]" },
      { from: "references", to: "report", kind: "wire", label: "references: list[Reference]" },
    ],
  },
  issues: [],
  fits: true,
  missing: [],
  brings: ["digest_source"],
  spec_yaml: "name: research-digest\n",
  staleness: [],
  lineage: null,
  birth: [],
  carried: null,
  predictions: null,
  prediction_record: null,
  trust: {
    level: "machine-confirmed",
    runs_with_evidence: 6,
    failed_runs: 0,
    approved_by: [],
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
  },
  archived_at: null,
};

function mountApi() {
  server.use(http.get(`${BASE}/api/workflow/${DETAIL.id}`, () => HttpResponse.json(DETAIL)));
}

describe("WorkflowLibraryDetailPage", () => {
  it("renders the title and summary", async () => {
    mountApi();
    render(<WorkflowLibraryDetailPage />);

    expect(await screen.findByRole("heading", { name: "Research digest" })).toBeInTheDocument();
    expect(
      screen.getByText("Turns undigested sources into written notes and references."),
    ).toBeInTheDocument();
  });

  it("shows archived and Restore once the workflow is archived from the page", async () => {
    mountApi();
    server.use(
      http.post(`${BASE}/api/workflow/${DETAIL.id}/archive`, () =>
        HttpResponse.json({
          ok: true,
          id: DETAIL.id,
          title: DETAIL.title,
          summary: DETAIL.summary,
          applicability: DETAIL.applicability,
          provisional: DETAIL.provisional,
          archived_at: "2026-08-25T20:00:00+00:00",
        }),
      ),
    );
    render(<WorkflowLibraryDetailPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Archive" }));
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Archive" }),
    );

    expect(await screen.findByText("archived")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore from the archive" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
  });

  it("renders the four steps in execution order, undigested first then report last", async () => {
    mountApi();
    render(<WorkflowLibraryDetailPage />);

    const ids = await screen.findAllByRole("heading", { level: 3 });
    expect(ids.map((el) => el.textContent)).toEqual([
      "undigested",
      "digests",
      "references",
      "report",
    ]);
  });

  it("names digests and references in the side by side caption", async () => {
    mountApi();
    render(<WorkflowLibraryDetailPage />);

    await screen.findByText("The steps");
    expect(
      screen.getAllByText(
        (_, element) =>
          element?.textContent === "3 stages, digests and references run side by side",
      ).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("Then, these 2 run side by side")).toBeInTheDocument();
  });

  it("shows the DigestResult table's own status field with its allowed values", async () => {
    mountApi();
    render(<WorkflowLibraryDetailPage />);

    await screen.findByText("A list of DigestResult, each with these fields");
    const statusRow = screen.getByText("status").closest("tr");
    expect(statusRow).toHaveTextContent("written, skipped");
  });

  it("names DigestReport and its three integer fields in what a run hands back", async () => {
    mountApi();
    render(<WorkflowLibraryDetailPage />);

    expect(await screen.findByText("What a run hands back")).toBeInTheDocument();
    expect(screen.getAllByText("One DigestReport with these fields").length).toBeGreaterThan(0);
    for (const name of ["written", "skipped", "requested"]) {
      expect(screen.getAllByText(name).length).toBeGreaterThan(0);
    }
  });

  it("links Latest run to the run inspector for this same workflow", async () => {
    mountApi();
    render(<WorkflowLibraryDetailPage />);

    expect(await screen.findByRole("link", { name: "Latest run" })).toHaveAttribute(
      "href",
      "/workflows/wf.research-digest.v1",
    );
  });

  it("shows an error sentence when the workflow cannot be opened", async () => {
    server.use(
      http.get(`${BASE}/api/workflow/${DETAIL.id}`, () =>
        HttpResponse.json({ message: "not found" }, { status: 404 }),
      ),
    );
    render(<WorkflowLibraryDetailPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not open wf.research-digest.v1.",
    );
  });
});
