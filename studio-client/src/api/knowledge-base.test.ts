import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/shared/test-support/msw/server";
import { graphResultFactory } from "@/test-support/factories/graph";

import { ApiError, knowledgeBaseApi, knowledgeBaseErrorMessage } from "./knowledge-base";

// A minimal-but-complete KnowledgeBaseSchema response: KnowledgeBaseSchemaSchema is STRICT (every
// field, mirroring pydantic's model_dump_json, which always emits defaults), so this fixture
// spells every one out rather than relying on zod defaults that don't exist here.
const SCHEMA = {
  version: "0.1",
  slug: "inbox",
  purpose: "",
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
  sources: { enabled: false, quote_verify: false, connectors: ["zotero"] },
  retrieval: { profile: "full", collection: "inbox", disclosure_mode: "strict", group_by: null },
  section_summaries: { leading_sentences: 2, inline_gate_chars: 1000 },
  embed_strategies: { active: null, config: {} },
  path_root: "parent",
};

// The vitest env pins NEXT_PUBLIC_KNOWLEDGE_BASE_API_URL to http://api.test (vitest.config.ts),
// so knowledgeBaseApi's base URL is that origin. Each test owns its surface via server.use;
// any unmocked request is a hard failure (onUnhandledFrame: "error").
const BASE = "http://api.test";

// One `coordination.jobs` row as GET /api/jobs/{id} answers it (core.cqrs.ports.Job).
const JOB = {
  id: 7,
  kind: "kb.refresh",
  key: "inbox",
  payload: { knowledge_base: "inbox", trigger: "manual" },
  state: "running",
  attempts: 1,
  max_attempts: 3,
  available_at: "2026-08-26T06:00:00+00:00",
  locked_at: "2026-08-26T06:00:01+00:00",
  locked_by: "studio:4211",
  lease_seconds: 1800,
  last_error: null,
  result: null,
  created_at: "2026-08-26T06:00:00+00:00",
  started_at: "2026-08-26T06:00:01+00:00",
  finished_at: null,
};

describe("knowledgeBaseApi", () => {
  it("parses a /api/graph response built from the factory", async () => {
    const graph = graphResultFactory.build();
    server.use(http.get(`${BASE}/api/graph`, () => HttpResponse.json(graph)));

    const result = await knowledgeBaseApi.graph({ page_type: "paper" });

    expect(result.nodes).toHaveLength(2);
    expect(result.edges).toHaveLength(1);
    expect(result.edges[0].src_id).toBe(result.nodes[0].id);
  });

  it("rejects a contract-violating response at the zod boundary", async () => {
    // `superseded` must be a boolean; the backend sending a string is exactly the
    // drift the runtime boundary exists to catch.
    server.use(
      http.get(`${BASE}/api/graph`, () =>
        HttpResponse.json({
          nodes: [
            { id: "paper:x", type: "paper", title: "X", status: "draft", superseded: "nope" },
          ],
          edges: [],
        }),
      ),
    );

    await expect(knowledgeBaseApi.graph()).rejects.toThrowError();
  });

  it("serialises only the provided filters into the query string", async () => {
    let seenUrl = "";
    server.use(
      http.get(`${BASE}/api/graph`, ({ request }) => {
        seenUrl = request.url;
        return HttpResponse.json(graphResultFactory.build());
      }),
    );

    await knowledgeBaseApi.graph({ rel: "outperforms" });

    const url = new URL(seenUrl);
    expect(url.searchParams.get("rel")).toBe("outperforms");
    expect(url.searchParams.has("page_type")).toBe(false);
  });

  it("scopes the fetch to one knowledge base via the knowledge_base param", async () => {
    let seenUrl = "";
    server.use(
      http.get(`${BASE}/api/graph`, ({ request }) => {
        seenUrl = request.url;
        return HttpResponse.json(graphResultFactory.build());
      }),
    );

    await knowledgeBaseApi.graph({ knowledge_base: "research" });

    const url = new URL(seenUrl);
    expect(url.searchParams.get("knowledge_base")).toBe("research");
  });

  it("surfaces an unknown knowledge base as a 404 ApiError", async () => {
    server.use(
      http.get(`${BASE}/api/graph`, () =>
        HttpResponse.json(
          { detail: "no knowledge base named 'missing' is registered" },
          { status: 404 },
        ),
      ),
    );

    await expect(
      knowledgeBaseApi.graph({ knowledge_base: "missing" }),
    ).rejects.toMatchObject({ status: 404 });
  });

  it("parses a /api/knowledge-bases response listing all four states", async () => {
    const body = {
      knowledge_bases: [
        {
          name: "research",
          registered: true,
          indexed: true,
          broken: false,
          path: "/c/research",
          description: "",
          pages: 12,
          page_types: ["paper"],
          last_indexed: "2026-08-11T09:00:00+00:00",
        },
        {
          name: "drafts",
          registered: true,
          indexed: false,
          broken: false,
          path: "/c/drafts",
          description: "",
          pages: null,
          page_types: [],
          last_indexed: null,
        },
      ],
    };
    server.use(http.get(`${BASE}/api/knowledge-bases`, () => HttpResponse.json(body)));

    const result = await knowledgeBaseApi.knowledgeBases();

    expect(result.knowledge_bases).toHaveLength(2);
    expect(result.knowledge_bases[0].indexed).toBe(true);
    expect(result.knowledge_bases[1].pages).toBeNull();
  });

  it("posts name/path/schema and parses the InitReport response", async () => {
    let seenBody: unknown;
    server.use(
      http.post(`${BASE}/api/knowledge-bases`, async ({ request }) => {
        seenBody = await request.json();
        return HttpResponse.json({
          written: ["knowledge-base-schema.json"],
          skipped: [],
          merged: [],
          forced: [],
        });
      }),
    );

    const report = await knowledgeBaseApi.createKnowledgeBase({
      name: "scratch",
      path: "/tmp/scratch",
      schema: { slug: "scratch", page_types: [{ page_type: "note" }] },
    });

    expect(report.written).toEqual(["knowledge-base-schema.json"]);
    expect(seenBody).toEqual({
      name: "scratch",
      path: "/tmp/scratch",
      schema: { slug: "scratch", page_types: [{ page_type: "note" }] },
      description: "",
      force: false,
    });
  });

  it("surfaces a create refusal as a 409 ApiError", async () => {
    server.use(
      http.post(`${BASE}/api/knowledge-bases`, () =>
        HttpResponse.json(
          {
            error: "name_collision",
            detail: "a knowledge base named 'scratch' is already registered",
          },
          { status: 409 },
        ),
      ),
    );

    await expect(
      knowledgeBaseApi.createKnowledgeBase({
        name: "scratch",
        path: "/tmp/scratch",
        schema: { slug: "scratch", page_types: [{ page_type: "note" }] },
      }),
    ).rejects.toMatchObject({ status: 409 });
  });

  it("parses a /api/knowledge-bases/{name}/schema response", async () => {
    server.use(http.get(`${BASE}/api/knowledge-bases/inbox/schema`, () => HttpResponse.json(SCHEMA)));

    const schema = await knowledgeBaseApi.knowledgeBaseSchema("inbox");

    expect(schema.slug).toBe("inbox");
    expect(schema.page_types).toHaveLength(1);
    expect(schema.sources.connectors).toEqual(["zotero"]);
  });

  it("parses a schema whose sources declare more than one connector", async () => {
    const schemaWithConnectors = {
      ...SCHEMA,
      sources: { enabled: true, quote_verify: false, connectors: ["zotero", "intake"] },
    };
    server.use(
      http.get(`${BASE}/api/knowledge-bases/inbox/schema`, () => HttpResponse.json(schemaWithConnectors)),
    );

    const schema = await knowledgeBaseApi.knowledgeBaseSchema("inbox");

    expect(schema.sources.connectors).toEqual(["zotero", "intake"]);
  });

  it("parses canonical_sections as either a bare slug or a declared-heading object", async () => {
    // engram.kb.domain.schema.CanonicalSection: a page type's canonical_sections entries are a bare
    // string (no declared heading) or {slug, heading} — both shapes are live on the wire.
    const schemaWithHeadings = {
      ...SCHEMA,
      page_types: [
        {
          ...SCHEMA.page_types[0],
          canonical_sections: ["context", { slug: "tl_dr", heading: "TL;DR" }],
        },
      ],
    };
    server.use(
      http.get(`${BASE}/api/knowledge-bases/inbox/schema`, () => HttpResponse.json(schemaWithHeadings)),
    );

    const schema = await knowledgeBaseApi.knowledgeBaseSchema("inbox");

    expect(schema.page_types[0].canonical_sections).toEqual([
      "context",
      { slug: "tl_dr", heading: "TL;DR" },
    ]);
  });

  it("parses a relation's qualifiers as groups of claim link field names", async () => {
    // engram.kb.domain.schema.RelationDecl serializes its qualifiers as a list of groups, each a list of names.
    const schemaWithQualifiers = {
      ...SCHEMA,
      relations: [
        {
          slug: "governs",
          domain: "note",
          range: "note",
          symmetric: false,
          qualifiers: [["applies_to", "governed_by"], ["court"]],
          description: "",
        },
      ],
    };
    server.use(
      http.get(`${BASE}/api/knowledge-bases/inbox/schema`, () => HttpResponse.json(schemaWithQualifiers)),
    );

    const schema = await knowledgeBaseApi.knowledgeBaseSchema("inbox");

    expect(schema.relations[0].qualifiers).toEqual([["applies_to", "governed_by"], ["court"]]);
  });

  it("answers the queued job id when the refresh route accepts", async () => {
    server.use(
      http.post(`${BASE}/api/knowledge-bases/inbox/refresh`, () =>
        HttpResponse.json({ job_id: 7 }, { status: 202, headers: { Location: "/api/jobs/7" } }),
      ),
    );

    const started = await knowledgeBaseApi.startRefresh("inbox");

    expect(started).toEqual({ jobId: 7, alreadyRunning: false });
  });

  it("reads a 409 from the refresh route as the run already underway, not an error", async () => {
    server.use(
      http.post(`${BASE}/api/knowledge-bases/inbox/refresh`, () =>
        HttpResponse.json(
          {
            error: "refresh_in_progress",
            detail: "a refresh run for 'inbox' is already queued or underway.",
            job_id: 4,
          },
          { status: 409 },
        ),
      ),
    );

    const started = await knowledgeBaseApi.startRefresh("inbox");

    expect(started).toEqual({ jobId: 4, alreadyRunning: true });
  });

  it("still throws on a 409 that is not an in-progress run", async () => {
    server.use(
      http.post(`${BASE}/api/knowledge-bases/stale/refresh`, () =>
        HttpResponse.json(
          {
            error: "broken_knowledge_base",
            detail: "knowledge base 'stale' is registered but its directory is unusable",
          },
          { status: 409 },
        ),
      ),
    );

    await expect(knowledgeBaseApi.startRefresh("stale")).rejects.toBeInstanceOf(ApiError);
  });

  it("still throws when the refresh route refuses an unknown knowledge base", async () => {
    server.use(
      http.post(`${BASE}/api/knowledge-bases/ghost/refresh`, () =>
        HttpResponse.json(
          { error: "unknown_knowledge_base", detail: "no knowledge base named 'ghost' is registered" },
          { status: 404 },
        ),
      ),
    );

    await expect(knowledgeBaseApi.startRefresh("ghost")).rejects.toBeInstanceOf(ApiError);
  });

  it("parses a job read", async () => {
    server.use(http.get(`${BASE}/api/jobs/7`, () => HttpResponse.json(JOB)));

    const job = await knowledgeBaseApi.readJob(7);

    expect(job.state).toBe("running");
    expect(job.key).toBe("inbox");
  });

  it("rejects a job whose state is not one the backend can send", async () => {
    server.use(
      http.get(`${BASE}/api/jobs/7`, () => HttpResponse.json({ ...JOB, state: "sideways" })),
    );

    await expect(knowledgeBaseApi.readJob(7)).rejects.toThrowError();
  });
});

describe("knowledgeBaseErrorMessage", () => {
  it("unwraps the {error, detail} body's detail sentence", () => {
    const error = new ApiError(
      404,
      JSON.stringify({
        error: "unknown_knowledge_base",
        detail: "no knowledge base named 'missing' is registered",
      }),
    );

    expect(knowledgeBaseErrorMessage(error)).toBe("no knowledge base named 'missing' is registered");
  });

  it("falls back to the raw message when the body is not JSON", () => {
    const error = new ApiError(500, "boom");

    expect(knowledgeBaseErrorMessage(error)).toBe("boom");
  });
});

// Keeping a run's value as a note: the two calls, and what each one puts on the wire. Neither
// carries an address for the value, only the reference the host minted, and a keep carries the
// preview's own digests back unchanged.
describe("note retention", () => {
  const ORIGIN = {
    id: "retention_draft",
    label: "Proposed knowledge note",
    run_id: "run-1",
    node_id: "present",
    turn: null,
    value_digest: "a".repeat(64),
    media_type: "application/json" as const,
  };

  const DRAFT = {
    page_type: "concept",
    title: "What the run concluded",
    summary: "One reading of what the evidence supports.",
    frontmatter: { status: "draft" },
    sections: [{ heading: "TL;DR", body: "It holds." }],
  };

  const PREVIEW = {
    knowledge_base: "notebook",
    note_id: "c-0123456789abcdef01234567",
    draft: DRAFT,
    markdown: "# What the run concluded\n",
    path: "Wiki/Concepts/0123456789abcdef01234567.md",
    read_ref: "page:c-0123456789abcdef01234567",
    schema_digest: "s".repeat(64),
    preview_digest: "p".repeat(64),
    warnings: [],
  };

  const KEPT = {
    outcome: "saved",
    knowledge_base: "notebook",
    note_id: "c-0123456789abcdef01234567",
    path: "Wiki/Concepts/0123456789abcdef01234567.md",
    read_ref: "page:c-0123456789abcdef01234567",
    detail: "indexed clean",
    warnings: [],
  };

  const KEEP_BODY = {
    idempotency_key: "key-1",
    origin: ORIGIN,
    draft: DRAFT,
    schema_digest: "s".repeat(64),
    preview_digest: "p".repeat(64),
  };

  it("previews against the named knowledge base, sending only the reference", async () => {
    const sent: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/knowledge-bases/notebook/notes/preview`, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(PREVIEW);
      }),
    );

    const preview = await knowledgeBaseApi.previewNote("notebook", ORIGIN);

    expect(sent).toEqual([{ origin: ORIGIN }]);
    expect(preview.path).toBe("Wiki/Concepts/0123456789abcdef01234567.md");
    expect(preview.note_id).toBe("c-0123456789abcdef01234567");
  });

  it("sends an edited draft whole, since a preview is of the note as it now reads", async () => {
    const sent: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/knowledge-bases/notebook/notes/preview`, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(PREVIEW);
      }),
    );

    await knowledgeBaseApi.previewNote("notebook", ORIGIN, { ...DRAFT, title: "Mine" });

    expect(sent).toEqual([{ origin: ORIGIN, edited_draft: { ...DRAFT, title: "Mine" } }]);
  });

  it("keeps a note by sending the request id, the draft and both digests back", async () => {
    const sent: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/knowledge-bases/notebook/notes`, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(KEPT);
      }),
    );

    const kept = await knowledgeBaseApi.retainNote("notebook", KEEP_BODY);

    expect(sent).toEqual([KEEP_BODY]);
    expect(kept.outcome).toBe("saved");
    expect(kept.read_ref).toBe("page:c-0123456789abcdef01234567");
  });

  it("rejects an outcome the backend cannot actually send", async () => {
    server.use(
      http.post(`${BASE}/api/knowledge-bases/notebook/notes`, () =>
        HttpResponse.json({ ...KEPT, outcome: "overwritten" }),
      ),
    );

    await expect(knowledgeBaseApi.retainNote("notebook", KEEP_BODY)).rejects.toThrowError();
  });
});
