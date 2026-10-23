import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { KnowledgeBaseListEntry } from "@/api/types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

// Imported after the mock so the list sees the mocked module.
import KnowledgeBasesPage from "./page";

// The Knowledge-bases station, exercised through knowledgeBaseApi alone, MSW answers the wire
// and `onUnhandledFrame: "error"` means a wrong path or verb fails here rather than
// falling through.

afterEach(cleanup);

const BASE = "http://api.test";
const KNOWLEDGE_BASES = `${BASE}/api/knowledge-bases`;

function entry(overrides: Partial<KnowledgeBaseListEntry> = {}): KnowledgeBaseListEntry {
  return {
    name: "research",
    registered: true,
    indexed: true,
    broken: false,
    path: "/knowledge-bases/research",
    description: "",
    pages: 12,
    page_types: ["paper"],
    last_indexed: "2026-08-11T09:00:00+00:00",
    ...overrides,
  };
}

// A registry that answers the CURRENT state: a create is only proven by the list the
// next fetch returns (React Query's invalidate-on-success).
function installRegistry(initial: KnowledgeBaseListEntry[]) {
  const registry = [...initial];
  let created: unknown = null;

  server.use(
    http.get(KNOWLEDGE_BASES, () => HttpResponse.json({ knowledge_bases: registry })),
    http.post(KNOWLEDGE_BASES, async ({ request }) => {
      const body = (await request.json()) as { name: string; path: string };
      created = body;
      registry.push(
        entry({
          name: body.name,
          path: body.path,
          indexed: false,
          pages: null,
          page_types: [],
          last_indexed: null,
        }),
      );
      return HttpResponse.json({
        written: ["knowledge-base-schema.json"],
        skipped: [],
        merged: [],
        forced: [],
      });
    }),
  );

  return { registry, createdBody: () => created };
}

describe("KnowledgeBasesPage", () => {
  it("names the station before the list arrives", () => {
    installRegistry([]);
    render(<KnowledgeBasesPage />);

    expect(screen.getByRole("heading", { level: 1, name: "Knowledge bases" })).toBeInTheDocument();
  });

  it("lists both registered-and-indexed and registered-only knowledge bases", async () => {
    installRegistry([
      entry(),
      entry({ name: "drafts", indexed: false, pages: null, page_types: [], last_indexed: null }),
    ]);
    render(<KnowledgeBasesPage />);

    expect(await screen.findByText("research")).toBeInTheDocument();
    expect(screen.getByText("drafts")).toBeInTheDocument();
    expect(screen.getByText(/^Indexed .+\.$/)).toBeInTheDocument();
    expect(screen.getByText("Not indexed yet.")).toBeInTheDocument();
  });

  it("names the summary sentence variants: empty, plain, and with broken and not indexed bases", async () => {
    installRegistry([]);
    const { unmount } = render(<KnowledgeBasesPage />);
    expect(await screen.findByText("No bases yet.")).toBeInTheDocument();
    unmount();

    server.use(http.get(KNOWLEDGE_BASES, () => HttpResponse.json({ knowledge_bases: [entry()] })));
    const single = render(<KnowledgeBasesPage />);
    expect(await screen.findByText("1 base.")).toBeInTheDocument();
    single.unmount();

    server.use(
      http.get(KNOWLEDGE_BASES, () =>
        HttpResponse.json({
          knowledge_bases: [
            entry({ name: "research" }),
            entry({
              name: "drafts",
              indexed: false,
              pages: null,
              page_types: [],
              last_indexed: null,
            }),
            entry({
              name: "moved-away",
              broken: true,
              indexed: false,
              path: "/knowledge-bases/gone",
            }),
            entry({ name: "fourth" }),
          ],
        }),
      ),
    );
    render(<KnowledgeBasesPage />);
    expect(await screen.findByText("4 bases, 1 broken, 1 not indexed.")).toBeInTheDocument();
  });

  it("creates a knowledge base from the dialog and shows it in the refreshed list", async () => {
    const { createdBody } = installRegistry([]);
    render(<KnowledgeBasesPage />);

    await screen.findByText("No bases yet.");

    await userEvent.click(screen.getByRole("button", { name: "New base" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "inbox");
    await userEvent.type(within(dialog).getByLabelText("Path"), "/knowledge-bases/inbox");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(createdBody()).toMatchObject({ name: "inbox", path: "/knowledge-bases/inbox" }),
    );
    expect(await screen.findByText("inbox")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("surfaces a name-collision refusal in the dialog and leaves it open", async () => {
    server.use(
      http.get(KNOWLEDGE_BASES, () => HttpResponse.json({ knowledge_bases: [entry()] })),
      http.post(KNOWLEDGE_BASES, () =>
        HttpResponse.json(
          {
            error: "name_collision",
            detail: "a knowledge base named 'research' is already registered",
          },
          { status: 409 },
        ),
      ),
    );
    render(<KnowledgeBasesPage />);

    await screen.findByText("research");
    await userEvent.click(screen.getByRole("button", { name: "New base" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "research");
    await userEvent.type(within(dialog).getByLabelText("Path"), "/knowledge-bases/research-2");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("already registered");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("opens the schema viewer for a registered knowledge base", async () => {
    installRegistry([entry()]);
    server.use(
      http.get(`${BASE}/api/knowledge-bases/research/schema`, () =>
        HttpResponse.json({
          version: "0.1",
          slug: "research",
          purpose: "",
          slug_conventions: "",
          page_types: [
            {
              page_type: "paper",
              id_prefix: "paper",
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
          sources: { enabled: false, quote_verify: false, connectors: ["zotero"] },
          retrieval: {
            profile: "full",
            collection: "research",
            disclosure_mode: "strict",
            group_by: null,
          },
          section_summaries: { leading_sentences: 2, inline_gate_chars: 1000 },
          embed_strategies: { active: null, config: {} },
          path_root: "parent",
        }),
      ),
    );
    render(<KnowledgeBasesPage />);

    await userEvent.click(await screen.findByRole("button", { name: "View schema" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText('"slug": "research"', { exact: false })).toBeInTheDocument();
  });
});
