import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { knowledgeBaseKeys } from "@/api/query-keys";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { KnowledgeBaseSchemaViewer } from "./schema-viewer";

afterEach(cleanup);

const BASE = "http://api.test";

const SCHEMA = {
  version: "0.1",
  slug: "inbox",
  purpose: "A capture inbox.",
  slug_conventions: "",
  page_types: [
    {
      page_type: "note",
      id_prefix: "note",
      folder: null,
      folders: [],
      files: [],
      frontmatter_fields: [],
      status_values: [],
      path_fields: [],
      canonical_sections: [],
      edge_fields: [],
      claims_enabled: false,
      disclosed: true,
    },
  ],
  relations: [],
  entity_types: [],
  claim_types: [],
  facets: [],
  claims: null,
  sources: { enabled: true, quote_verify: false, connectors: ["youtube"] },
  retrieval: { profile: "full", collection: "inbox", disclosure_mode: "strict", group_by: null },
  section_summaries: { leading_sentences: 2, inline_gate_chars: 1000 },
  embed_strategies: { active: null, config: {} },
  path_root: "parent",
};

describe("KnowledgeBaseSchemaViewer", () => {
  it("renders nothing when no name is selected", () => {
    render(<KnowledgeBaseSchemaViewer name={null} onClose={vi.fn()} />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("fetches and renders the selected knowledge base's schema", async () => {
    server.use(http.get(`${BASE}/api/knowledge-bases/inbox/schema`, () => HttpResponse.json(SCHEMA)));
    render(<KnowledgeBaseSchemaViewer name="inbox" onClose={vi.fn()} />);

    expect(screen.getByRole("dialog")).toHaveAccessibleName("inbox");
    expect(screen.getByText("Loading schema...")).toBeInTheDocument();

    await waitFor(() =>
      expect(screen.getByText(/"purpose": "A capture inbox\."/)).toBeInTheDocument(),
    );
  });

  it("renders a schema whose canonical_sections mixes bare slugs and declared headings", async () => {
    // engram.kb.domain.schema.CanonicalSection widened canonical_sections from plain strings to
    // string | {slug, heading}, a knowledge base with a declared heading (e.g. the bundled
    // research schema's tl_dr -> "TL;DR") must still parse, not throw a ZodError at this boundary.
    const schemaWithHeading = {
      ...SCHEMA,
      page_types: [
        {
          ...SCHEMA.page_types[0],
          canonical_sections: ["context", { slug: "tl_dr", heading: "TL;DR" }],
        },
      ],
    };
    server.use(
      http.get(`${BASE}/api/knowledge-bases/inbox/schema`, () => HttpResponse.json(schemaWithHeading)),
    );
    render(<KnowledgeBaseSchemaViewer name="inbox" onClose={vi.fn()} />);

    await waitFor(() => expect(screen.getByText(/"heading": "TL;DR"/)).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the unwrapped refusal message on a 404", async () => {
    server.use(
      http.get(`${BASE}/api/knowledge-bases/missing/schema`, () =>
        HttpResponse.json(
          {
            error: "unknown_knowledge_base",
            detail: "no knowledge base named 'missing' is registered",
          },
          { status: 404 },
        ),
      ),
    );
    render(<KnowledgeBaseSchemaViewer name="missing" onClose={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no knowledge base named 'missing' is registered",
    );
  });

  it("closes via the Close button and via Escape", async () => {
    server.use(http.get(`${BASE}/api/knowledge-bases/inbox/schema`, () => HttpResponse.json(SCHEMA)));
    const onClose = vi.fn();
    render(<KnowledgeBaseSchemaViewer name="inbox" onClose={onClose} />);

    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("reads the schema under the knowledge-base topic root", async () => {
    server.use(
      http.get(`${BASE}/api/knowledge-bases/research/schema`, () => HttpResponse.json(SCHEMA)),
    );
    const { queryClient } = render(
      <KnowledgeBaseSchemaViewer name="research" onClose={() => {}} />,
    );

    await waitFor(() =>
      expect(queryClient.getQueryState(knowledgeBaseKeys.schema("research"))).toBeDefined(),
    );
  });
});
