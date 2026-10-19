import { screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { LibraryEntry } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the library sees the mocked module.
import { WorkflowLibrary } from "./workflow-library";

// The library screen itself: the header's summary sentence, the search filter, the archived
// toggle, and the run route's own network round trip through a live card. A card's own anatomy
// (its fact row, its footer sentence) is covered in `library-card.test.tsx`, which needs none of
// this file's MSW plumbing.

const BASE = "http://studio.test";

function cardFixture(overrides: Partial<LibraryEntry> = {}): LibraryEntry {
  return {
    id: "wf.sales.v1",
    title: "Fetch sales",
    summary: "Pull last quarter's sales into one report.",
    provisional: false,
    score: 0.9,
    trust: {
      level: "machine-confirmed",
      runs_with_evidence: 3,
      failed_runs: 0,
      approved_by: [],
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
    },
    needs: [],
    missing: [],
    brings: [],
    version: 1,
    archived_at: null,
    shape: { steps: 4, person_gates: 1, agent_gates: 1 },
    runs: { last: null },
    ...overrides,
  };
}

// The setup a Run opens reads the workflow itself, for the parameters it declares.
function workflowFixture() {
  return {
    ok: true,
    id: "wf.sales.v1",
    name: "sales",
    version: 1,
    description: "",
    title: "Fetch sales",
    summary: "Pull last quarter's sales into one report.",
    applicability: "",
    provisional: false,
    graph: {
      name: "sales",
      version: 1,
      nodes: [
        {
          key: "param:quarter",
          id: "quarter",
          kind: "param",
          signature: "string",
          detail: "",
          tools: [],
          is_output: false,
          unknown: false,
          brought: false,
          briefing: "",
          input_schemas: {},
          output_schema: { type: "string", minLength: 1 },
          source: "",
        },
      ],
      edges: [],
    },
    issues: [],
    fits: true,
    missing: [],
    brings: [],
    spec_yaml: "name: sales\n",
    staleness: [],
    lineage: null,
    birth: null,
    carried: null,
    predictions: null,
    prediction_record: null,
    trust: cardFixture().trust,
    archived_at: null,
    presentation: null,
  };
}

function mountApi(
  overrides: { cards?: LibraryEntry[]; archivedCards?: LibraryEntry[] } = {},
): void {
  server.use(
    http.get(`${BASE}/api/library`, ({ request }) => {
      const archived = new URL(request.url).searchParams.get("archived") === "true";
      return HttpResponse.json(
        archived ? (overrides.archivedCards ?? []) : (overrides.cards ?? [cardFixture()]),
      );
    }),
    http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
    http.get(`${BASE}/api/workflow/:id`, () => HttpResponse.json(workflowFixture())),
  );
}

describe("WorkflowLibrary", () => {
  afterEach(() => {
    routerPush.mockClear();
  });

  it("renders a card per saved workflow, with its id, version, and summary", async () => {
    mountApi();
    render(<WorkflowLibrary />);

    expect(await screen.findByRole("link", { name: "Fetch sales" })).toHaveAttribute(
      "href",
      "/workflows/library/wf.sales.v1",
    );
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("Pull last quarter's sales into one report.")).toBeInTheDocument();
  });

  it("states the summary sentence, split by trust level in order, zero parts dropped", async () => {
    mountApi({
      cards: [
        cardFixture({
          id: "wf.a.v1",
          title: "A",
          trust: { ...cardFixture().trust, level: "approved" },
        }),
        cardFixture({
          id: "wf.b.v1",
          title: "B",
          trust: { ...cardFixture().trust, level: "approved" },
        }),
        cardFixture({
          id: "wf.c.v1",
          title: "C",
          trust: { ...cardFixture().trust, level: "machine-confirmed" },
        }),
      ],
    });
    render(<WorkflowLibrary />);

    expect(
      await screen.findByText("3 workflows, 2 approved, 1 confirmed by runs."),
    ).toBeInTheDocument();
  });

  it("states the summary sentence in the singular for one workflow", async () => {
    mountApi({
      cards: [cardFixture({ trust: { ...cardFixture().trust, level: "unverified" } })],
    });
    render(<WorkflowLibrary />);

    expect(await screen.findByText("1 workflow, 1 not verified.")).toBeInTheDocument();
  });

  it("narrows the grid as you type, over title, id, and summary", async () => {
    mountApi({
      cards: [
        cardFixture({ id: "wf.sales.v1", title: "Fetch sales" }),
        cardFixture({
          id: "wf.lit-check.v5",
          title: "Literature check",
          summary: "Checks a claim.",
        }),
      ],
    });
    render(<WorkflowLibrary />);

    await screen.findByRole("link", { name: "Fetch sales" });
    await userEvent.type(screen.getByRole("textbox", { name: "Find a workflow" }), "literature");

    expect(screen.queryByRole("link", { name: "Fetch sales" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Literature check" })).toBeInTheDocument();
  });

  it("shows a plain message, no cards, when the search matches nothing", async () => {
    mountApi();
    render(<WorkflowLibrary />);

    await screen.findByRole("link", { name: "Fetch sales" });
    await userEvent.type(
      screen.getByRole("textbox", { name: "Find a workflow" }),
      "nothing matches this",
    );

    expect(screen.getByText("No workflow matches your search.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Fetch sales" })).not.toBeInTheDocument();
  });

  it("opens the workflow on a card-body click", async () => {
    mountApi();
    render(<WorkflowLibrary />);

    await screen.findByRole("link", { name: "Fetch sales" });
    await userEvent.click(screen.getByText("Pull last quarter's sales into one report."));

    expect(routerPush).toHaveBeenCalledWith("/workflows/library/wf.sales.v1");
  });

  it("sets a run up, then runs through the session route and navigates on success", async () => {
    mountApi();
    let createdBody: unknown = null;
    let renamedTo = "";
    let runPosted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, async ({ request }) => {
        createdBody = await request.json();
        return HttpResponse.json({
          session: {
            id: "sess-new",
            project_id: "proj-1",
            title: "session sess-new",
            started_at: "2026-08-23T10:00:00Z",
            last_activity_at: "2026-08-23T10:00:00Z",
            archived: false,
            answering: false,
            turn_count: 0,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_read_tokens: 0,
              cache_write_tokens: 0,
            },
            kind: "runner",
            cost_usd: "0",
            context: null,
          },
        });
      }),
      http.patch(`${BASE}/api/sessions/sess-new`, async ({ request }) => {
        renamedTo = ((await request.json()) as { title: string }).title;
        return HttpResponse.json({
          session: {
            id: "sess-new",
            project_id: "proj-1",
            title: renamedTo,
            started_at: "2026-08-23T10:00:00Z",
            last_activity_at: "2026-08-23T10:00:00Z",
            archived: false,
            answering: false,
            turn_count: 0,
            usage: {
              input_tokens: 0,
              output_tokens: 0,
              cache_read_tokens: 0,
              cache_write_tokens: 0,
            },
            kind: "runner",
            cost_usd: "0",
            context: null,
          },
        });
      }),
      http.post(`${BASE}/api/sessions/sess-new/run`, async ({ request }) => {
        runPosted = await request.json();
        return HttpResponse.json({ ok: true, running: "wf.sales.v1" });
      }),
    );
    render(<WorkflowLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    expect(createdBody).toBeNull();

    await userEvent.type(await within(dialog).findByLabelText("quarter"), "Q1");
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    await waitFor(() =>
      expect(runPosted).toEqual({
        workflow_id: "wf.sales.v1",
        arguments: { quarter: "Q1" },
        run_title: "Fetch sales",
      }),
    );
    expect(createdBody).toEqual({ kind: "runner" });
    expect(renamedTo).toBe("Fetch sales");
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith("/workflows/wf.sales.v1"));
  });

  it("keeps the run route's own refusal in the setup, without navigating", async () => {
    mountApi();
    server.use(
      http.post(`${BASE}/api/sessions`, () =>
        HttpResponse.json({ detail: "wf.sales.v1 is still running" }, { status: 500 }),
      ),
    );
    render(<WorkflowLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(await within(dialog).findByLabelText("quarter"), "Q1");
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "wf.sales.v1 is still running",
    );
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("shows the empty state with the new-workflow action when the library is bare", async () => {
    mountApi({ cards: [] });
    render(<WorkflowLibrary />);

    expect(await screen.findAllByText("No workflows yet.")).not.toHaveLength(0);
    const newWorkflowLinks = screen.getAllByRole("link", { name: "New workflow" });
    expect(newWorkflowLinks.length).toBeGreaterThan(0);
    for (const link of newWorkflowLinks) {
      expect(link).toHaveAttribute("href", "/chat");
    }
  });

  it("says no workflows yet in the header's own summary sentence when the library is bare", async () => {
    mountApi({ cards: [] });
    render(<WorkflowLibrary />);

    const heading = await screen.findByRole("heading", { level: 1, name: "Library" });
    const header = heading.parentElement as HTMLElement;
    expect(await within(header).findByText("No workflows yet.")).toBeInTheDocument();
  });

  it("shows the library's own error", async () => {
    server.use(
      http.get(`${BASE}/api/library`, () =>
        HttpResponse.json({ detail: "the library is unreachable" }, { status: 500 }),
      ),
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
    );
    render(<WorkflowLibrary />);

    expect(await screen.findByRole("alert")).toHaveTextContent("the library is unreachable");
  });

  it("spells the level out in words for every level, never leaving it to color alone", async () => {
    mountApi({
      cards: [
        cardFixture({
          id: "wf.unverified.v1",
          title: "Never run",
          trust: {
            level: "unverified",
            runs_with_evidence: 0,
            failed_runs: 0,
            approved_by: [],
            structural_grades: 0,
            structural_mean: null,
            structural_last: null,
          },
        }),
        cardFixture({
          id: "wf.confirmed.v1",
          title: "Ran fine",
          trust: {
            level: "machine-confirmed",
            runs_with_evidence: 5,
            failed_runs: 2,
            approved_by: [],
            structural_grades: 0,
            structural_mean: null,
            structural_last: null,
          },
        }),
        cardFixture({
          id: "wf.approved.v1",
          title: "Approved",
          trust: {
            level: "approved",
            runs_with_evidence: 1,
            failed_runs: 0,
            approved_by: ["alex"],
            structural_grades: 0,
            structural_mean: null,
            structural_last: null,
          },
        }),
      ],
    });
    render(<WorkflowLibrary />);

    await screen.findByRole("link", { name: "Never run" });

    expect(screen.getByText("not verified")).toBeInTheDocument();
    expect(screen.getByText("confirmed by runs")).toBeInTheDocument();
    expect(screen.getByText((_, element) => element?.textContent === "5 runs")).toBeInTheDocument();
    expect(
      screen.getByText((_, element) => element?.textContent === "failed 2"),
    ).toBeInTheDocument();
    expect(screen.getByText("approved")).toBeInTheDocument();
    expect(screen.getByText("by alex")).toBeInTheDocument();
  });

  it("never renders the static provisional flag, however the wire sets it", async () => {
    mountApi({
      cards: [
        cardFixture({ provisional: true }),
        cardFixture({ id: "wf.other.v1", title: "Other", provisional: false }),
      ],
    });
    render(<WorkflowLibrary />);

    await screen.findByRole("link", { name: "Fetch sales" });

    expect(screen.queryByText("provisional")).not.toBeInTheDocument();
  });

  it("opens the approve confirm and states its sentence in plain punctuation", async () => {
    mountApi();
    render(<WorkflowLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Approve it" }));

    const sentence = screen.getByText(
      "Your name goes on record as approving this workflow. There are no accounts here, so nothing fills it in for you.",
    );
    expect(sentence).toBeInTheDocument();
    expect(sentence.textContent).toMatch(/^[A-Za-z0-9 ,.?()]*$/);
    expect(screen.getByRole("textbox", { name: "Your name" })).toHaveValue("");
  });

  it("approves a workflow, posting the name and note, and shows it approved", async () => {
    mountApi();
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/workflows/wf.sales.v1/approve`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({
          ok: true,
          trust: {
            level: "approved",
            runs_with_evidence: 3,
            failed_runs: 0,
            approved_by: ["owner"],
            structural_grades: 0,
            structural_mean: null,
            structural_last: null,
          },
        });
      }),
    );
    render(<WorkflowLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Approve it" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Your name" }), "owner");
    await userEvent.type(screen.getByRole("textbox", { name: "Note (optional)" }), "looks right");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(posted).toEqual({ by: "owner", note: "looks right" }));
    expect(await screen.findByText("approved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("shows the approve route's own error without closing the confirm", async () => {
    mountApi();
    server.use(
      http.post(`${BASE}/api/workflows/wf.sales.v1/approve`, () =>
        HttpResponse.json({ detail: "an approval needs a name" }, { status: 400 }),
      ),
    );
    render(<WorkflowLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Approve it" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Your name" }), "owner");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("an approval needs a name")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("the Show archived toggle lists archived cards, with Restore and no Run", async () => {
    mountApi({
      cards: [cardFixture()],
      archivedCards: [
        cardFixture({
          id: "wf.retired.v1",
          title: "Retired report",
          archived_at: "2026-08-01T00:00:00Z",
        }),
      ],
    });
    render(<WorkflowLibrary />);

    await screen.findByRole("link", { name: "Fetch sales" });
    await userEvent.click(screen.getByRole("button", { name: "Show archived" }));

    expect(await screen.findByRole("link", { name: "Retired report" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Restore" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Run" })).toHaveLength(1);
  });

  it("approves an archived card and updates that same card in place", async () => {
    mountApi({
      cards: [],
      archivedCards: [
        cardFixture({
          id: "wf.retired.v1",
          title: "Retired report",
          archived_at: "2026-08-01T00:00:00Z",
        }),
      ],
    });
    server.use(
      http.post(`${BASE}/api/workflows/wf.retired.v1/approve`, () =>
        HttpResponse.json({
          ok: true,
          trust: {
            level: "approved",
            runs_with_evidence: 3,
            failed_runs: 0,
            approved_by: ["owner"],
            structural_grades: 0,
            structural_mean: null,
            structural_last: null,
          },
        }),
      ),
    );
    render(<WorkflowLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Show archived" }));
    await userEvent.click(await screen.findByRole("button", { name: "Approve it" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Your name" }), "owner");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("approved")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Retired report" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve it" })).not.toBeInTheDocument();
  });

  it("restores an archived card, posting to the restore route", async () => {
    mountApi({
      cards: [],
      archivedCards: [
        cardFixture({
          id: "wf.retired.v1",
          title: "Retired report",
          archived_at: "2026-08-01T00:00:00Z",
        }),
      ],
    });
    let restorePosted = false;
    server.use(
      http.post(`${BASE}/api/workflow/wf.retired.v1/restore`, () => {
        restorePosted = true;
        return HttpResponse.json({
          ok: true,
          id: "wf.retired.v1",
          title: "Retired report",
          summary: "Pull last quarter's sales into one report.",
          applicability: "",
          provisional: false,
          archived_at: null,
        });
      }),
    );
    render(<WorkflowLibrary />);

    await userEvent.click(await screen.findByRole("button", { name: "Show archived" }));
    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));

    await waitFor(() => expect(restorePosted).toBe(true));
  });

});
