import { fireEvent, screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { GraphNode, WorkflowDetail } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the component sees the mocked module.
import { WorkflowAbout } from "./workflow-about";

const BASE = "http://studio.test";

function sessionFixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "sess-9",
    project_id: "",
    title: "",
    started_at: "2026-08-25T10:00:00Z",
    last_activity_at: "2026-08-25T10:00:00Z",
    archived: false,
    answering: false,
    turn_count: 0,
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 },
    kind: "runner",
    cost_usd: "0",
    context: null,
    ...overrides,
  };
}

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

function detailFixture(overrides: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    ok: true,
    id: "wf.research-digest.v1",
    name: "research-digest",
    version: 1,
    description: "Digests every undigested source into the field's own notes.",
    title: "Research digest",
    summary: "Turns undigested sources into written notes.",
    applicability:
      "Use for a field with a live knowledge base. Do not use for a one off reading list.",
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
          output_schema: { type: "string", minLength: 1 },
        }),
        node({
          id: "undigested",
          kind: "run",
          detail: "Lists items not yet digested.",
          work: "list_undigested",
        }),
        node({
          id: "digests",
          kind: "map",
          detail: "Digests each undigested item.",
          brought: true,
          work: "digest_source",
        }),
        node({
          id: "report",
          kind: "run",
          detail: "Composes the final report.",
          is_output: true,
          work: "compose_report",
          output_schema: DIGEST_REPORT,
        }),
      ],
      edges: [
        { from: "param:collection", to: "undigested", kind: "wire", label: "collection: string" },
        { from: "undigested", to: "digests", kind: "map", label: "each source: DigestResult" },
        { from: "digests", to: "report", kind: "wire", label: "digests: list[DigestResult]" },
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
      runs_with_evidence: 4,
      failed_runs: 0,
      approved_by: [],
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
    },
    archived_at: null,
    spend: { cost_usd: "0", unpriced: 0, runs: 0 },
    ...overrides,
  };
}

describe("WorkflowAbout", () => {
  afterEach(() => {
    routerPush.mockClear();
  });

  it("shows the title, summary, and Latest run link to the run inspector", () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    expect(screen.getByRole("heading", { name: "Research digest" })).toBeInTheDocument();
    expect(screen.getByText("Turns undigested sources into written notes.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Latest run" })).toHaveAttribute(
      "href",
      "/workflows/wf.research-digest.v1",
    );
  });

  it("falls back to the id for the title when the card carries none", () => {
    render(<WorkflowAbout detail={detailFixture({ title: "" })} />);

    expect(screen.getByRole("heading", { name: "wf.research-digest.v1" })).toBeInTheDocument();
  });

  it("sets a run up before starting it, then invalidates runs and opens the inspector", async () => {
    let created = false;
    let renamed: unknown = null;
    let ran: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions`, () => {
        created = true;
        return HttpResponse.json({ session: sessionFixture() });
      }),
      http.patch(`${BASE}/api/sessions/sess-9`, async ({ request }) => {
        renamed = await request.json();
        return HttpResponse.json({ session: sessionFixture({ title: "Research digest" }) });
      }),
      http.post(`${BASE}/api/sessions/sess-9/run`, async ({ request }) => {
        ran = await request.json();
        return HttpResponse.json({ ok: true, running: "wf.research-digest.v1" });
      }),
    );
    render(<WorkflowAbout detail={detailFixture()} />);

    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    expect(created).toBe(false);

    await userEvent.type(within(dialog).getByLabelText("collection"), "research");
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    await waitFor(() =>
      expect(routerPush).toHaveBeenCalledWith("/workflows/wf.research-digest.v1"),
    );
    expect(created).toBe(true);
    expect(renamed).toEqual({ title: "Research digest" });
    expect(ran).toEqual({
      workflow_id: "wf.research-digest.v1",
      arguments: { collection: "research" },
      run_title: "Research digest",
    });
  });

  it("reads as busy in the setup while the run is starting", async () => {
    server.use(
      http.post(`${BASE}/api/sessions`, async () => {
        await new Promise((resolve) => setTimeout(resolve, 250));
        return HttpResponse.json({ session: sessionFixture() });
      }),
    );
    render(<WorkflowAbout detail={detailFixture()} />);

    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("collection"), "research");
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    // The setup stays open while the start is in flight, and its own button reads as busy.
    expect(await within(dialog).findByRole("button", { name: "Starting" })).toBeDisabled();
  });

  it("disables Run with the missing step count in its title when the workflow does not fit", () => {
    render(<WorkflowAbout detail={detailFixture({ fits: false, missing: ["digest_source"] })} />);

    const runButton = screen.getByRole("button", { name: "Run" });
    expect(runButton).toBeDisabled();
    expect(runButton).toHaveAttribute("title", "Cannot run, 1 step is missing.");
  });

  it("shows the archived badge instead of Run for an archived workflow", () => {
    render(<WorkflowAbout detail={detailFixture({ archived_at: "2026-08-01T00:00:00Z" })} />);

    expect(screen.getByText("archived")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
  });

  it("reads the description and, when non empty, the applicability under Use it when", () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    expect(
      screen.getByText("Digests every undigested source into the field's own notes."),
    ).toBeInTheDocument();
    expect(screen.getByText("Use it when")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Use for a field with a live knowledge base. Do not use for a one off reading list.",
      ),
    ).toBeInTheDocument();
  });

  it("omits Use it when for an empty applicability", () => {
    render(<WorkflowAbout detail={detailFixture({ applicability: "" })} />);

    expect(screen.queryByText("Use it when")).not.toBeInTheDocument();
  });

  it("shows the shape facts, version, and runs with evidence off the graph and the trust record", () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    expect(screen.getByText("Steps")).toBeInTheDocument();
    expect(screen.getByText("Person gates")).toBeInTheDocument();
    expect(screen.getByText("Orchestrator gates")).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("Runs completed").nextElementSibling).toHaveTextContent("4");
  });

  it("shows the running spend over however many runs were priced", () => {
    render(
      <WorkflowAbout
        detail={detailFixture({ spend: { cost_usd: "1.23", unpriced: 0, runs: 3 } })}
      />,
    );

    expect(screen.getByText("Spend")).toBeInTheDocument();
    expect(screen.getByText("$1.23 over 3 runs")).toBeInTheDocument();
  });

  it("reads the spend fact as nothing yet when no run has been priced", () => {
    render(
      <WorkflowAbout detail={detailFixture({ spend: { cost_usd: "0", unpriced: 0, runs: 0 } })} />,
    );

    expect(screen.getByText("nothing yet")).toBeInTheDocument();
  });

  it("shows Steps it carries and Steps this app provides, deduplicated, omitted when empty", () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    expect(screen.getByText("Steps it carries")).toBeInTheDocument();
    expect(screen.getAllByText("digest_source").length).toBeGreaterThan(0);
    expect(screen.getByText("Steps this app provides")).toBeInTheDocument();
    expect(screen.getAllByText("list_undigested").length).toBeGreaterThan(0);
    expect(screen.getAllByText("compose_report").length).toBeGreaterThan(0);
  });

  it("omits Steps it carries when the workflow brings nothing", () => {
    render(<WorkflowAbout detail={detailFixture({ brings: [] })} />);

    expect(screen.queryByText("Steps it carries")).not.toBeInTheDocument();
  });

  it("shows Missing here chips in the warning tone when the workflow carries any", () => {
    render(<WorkflowAbout detail={detailFixture({ fits: false, missing: ["digest_source"] })} />);

    expect(screen.getAllByText("Missing here").length).toBeGreaterThan(0);
  });

  it("lists every parameter, its type, required or optional, and its meaning", () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    // Scoped to the parameters card: the map above it draws the same parameter and its type.
    const card = document.getElementById("workflow-about-parameters");
    if (card === null) throw new Error("the parameters card is missing");
    const table = within(card);
    expect(table.getByText("1 parameter")).toBeInTheDocument();
    expect(table.getByText("collection")).toBeInTheDocument();
    expect(table.getByText("string")).toBeInTheDocument();
    expect(table.getByText("required")).toBeInTheDocument();
    expect(table.getByText("the knowledge base to draw from")).toBeInTheDocument();
  });

  it("reads optional off the parameter's own required flag, not off its signature text", () => {
    const detail = detailFixture({
      graph: {
        ...detailFixture().graph,
        nodes: detailFixture().graph.nodes.map((n) =>
          n.id === "collection" ? { ...n, required: false, signature: "string" } : n,
        ),
      },
    });
    render(<WorkflowAbout detail={detail} />);

    expect(screen.getByText("optional")).toBeInTheDocument();
  });

  it("ignores a signature that still spells the suffix out, because the flag is the fact", () => {
    const detail = detailFixture({
      graph: {
        ...detailFixture().graph,
        nodes: detailFixture().graph.nodes.map((n) =>
          n.id === "collection" ? { ...n, required: true, signature: "string (optional)" } : n,
        ),
      },
    });
    render(<WorkflowAbout detail={detail} />);

    // Scoped to the parameters card: the map above it draws the same parameter and its type.
    const card = document.getElementById("workflow-about-parameters");
    if (card === null) throw new Error("the parameters card is missing");
    const table = within(card);
    expect(table.getByText("required")).toBeInTheDocument();
    expect(table.queryByText("optional")).not.toBeInTheDocument();
  });

  it("says this workflow takes no parameters when it carries none", () => {
    const withoutParam = detailFixture();
    const detail = {
      ...withoutParam,
      graph: {
        ...withoutParam.graph,
        nodes: withoutParam.graph.nodes.filter((n) => n.kind !== "param"),
      },
    };
    render(<WorkflowAbout detail={detail} />);

    expect(screen.getByText("This workflow takes no parameters.")).toBeInTheDocument();
  });

  it("renders the step spine card", () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    expect(screen.getByText("The steps")).toBeInTheDocument();
  });

  it("draws the map above everything, open by default, and lets a reader collapse it", async () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    const toggle = screen.getByRole("button", { name: /The map/ });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Click a node to read its step.")).toBeInTheDocument();

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("gives every step row an id the map can jump to", () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    expect(document.getElementById("step-report")).not.toBeNull();
  });

  it("opens the map full screen as the bare drawing, and Escape leaves it", async () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    await userEvent.click(screen.getByRole("button", { name: "Full screen" }));
    const dialog = screen.getByRole("dialog", { name: /The map, full screen/ });
    expect(dialog).toBeInTheDocument();
    // Nothing but the drawing: no buttons, no pills, no legend on it.
    expect(within(dialog).queryAllByRole("button")).toHaveLength(0);
    expect(within(dialog).queryByText(/Circle is a step/)).not.toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("carries the inline orientation pick into full screen", async () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    await userEvent.click(screen.getByRole("button", { name: "Across" }));
    await userEvent.click(screen.getByRole("button", { name: "Full screen" }));
    const dialog = screen.getByRole("dialog", { name: /The map, full screen/ });
    // The bare drawing has no pills to read, so the pick shows in how it laid out: the full
    // screen canvas's cards carry the across direction's left/right handles.
    await waitFor(() => {
      const handle = dialog.querySelector('[data-node="undigested"] .react-flow__handle-left');
      expect(handle).not.toBeNull();
    });
  });

  it("folds every section behind its title", async () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    for (const title of ["What it does", "What it takes", "The steps", "What a run hands back"]) {
      const toggle = screen.getByRole("button", { name: title });
      expect(toggle).toHaveAttribute("aria-expanded", "true");
      await userEvent.click(toggle);
      expect(toggle).toHaveAttribute("aria-expanded", "false");
    }
    expect(screen.queryByText("Use it when")).not.toBeInTheDocument();
  });

  it("edits the card's own words in place through the PATCH route", async () => {
    let patched: unknown = null;
    server.use(
      http.patch(`${BASE}/api/workflow/:id/card`, async ({ request, params }) => {
        patched = await request.json();
        return HttpResponse.json({
          ok: true,
          id: params.id,
          title: "Digest new sources",
          summary: "Reads every new source and writes a short digest of each.",
          applicability: "Run it on a schedule, once per knowledge base.",
          provisional: false,
        });
      }),
    );
    render(<WorkflowAbout detail={detailFixture()} />);

    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const titleBox = screen.getByRole("textbox", { name: "Title" });
    await userEvent.clear(titleBox);
    await userEvent.type(titleBox, "Digest new sources");
    const summaryBox = screen.getByRole("textbox", { name: "Summary" });
    await userEvent.clear(summaryBox);
    await userEvent.type(summaryBox, "Reads every new source and writes a short digest of each.");
    const applicabilityBox = screen.getByRole("textbox", { name: "Applicability" });
    await userEvent.clear(applicabilityBox);
    await userEvent.type(applicabilityBox, "Run it on a schedule, once per knowledge base.");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(patched).toEqual({
        title: "Digest new sources",
        summary: "Reads every new source and writes a short digest of each.",
        applicability: "Run it on a schedule, once per knowledge base.",
        presentation: { purpose: "", produces: "", owner_participation: "", not_for: "" },
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("cancelling an edit closes the form without saving", async () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Title" }), " abandoned");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("textbox", { name: "Title" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("archives through the archive route only after the confirm", async () => {
    let archived = 0;
    server.use(
      http.post(`${BASE}/api/workflow/:id/archive`, ({ params }) => {
        archived += 1;
        return HttpResponse.json({
          ok: true,
          id: params.id,
          title: "Research digest",
          summary: "",
          applicability: "",
          provisional: false,
          archived_at: "2026-08-25T20:00:00+00:00",
        });
      }),
    );
    render(<WorkflowAbout detail={detailFixture()} />);

    await userEvent.click(screen.getByRole("button", { name: "Archive" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Archive this workflow?");
    expect(archived).toBe(0);

    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(archived).toBe(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("offers Restore instead of Run and Archive for an archived workflow", () => {
    render(<WorkflowAbout detail={detailFixture({ archived_at: "2026-08-25T20:00:00+00:00" })} />);

    expect(screen.getByRole("button", { name: "Restore from the archive" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });

  it("pushes a history entry on a node jump, so Back returns to the page as it was", async () => {
    const { container } = render(<WorkflowAbout detail={detailFixture()} />);
    const card = container.querySelector('[data-node="report"]');
    if (card === null) throw new Error("the map did not draw the report node");

    fireEvent.click(card);
    expect(window.location.hash).toBe("#step-report");

    window.history.back();
    await waitFor(() => expect(window.location.hash).toBe(""));
  });

  it("names the output node and reads its own schema for what a run hands back", () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    expect(screen.getByText("What a run hands back")).toBeInTheDocument();
    expect(screen.getByText("from the step")).toBeInTheDocument();
    expect(screen.getAllByText("report").length).toBeGreaterThan(0);
    expect(screen.getAllByText("One DigestReport with these fields").length).toBeGreaterThan(0);
  });

  it("says this comes from the output node when it is unknown to this app", () => {
    const withUnknownOutput = detailFixture();
    const detail = {
      ...withUnknownOutput,
      graph: {
        ...withUnknownOutput.graph,
        nodes: withUnknownOutput.graph.nodes.map((n) =>
          n.id === "report" ? { ...n, unknown: true } : n,
        ),
      },
    };
    render(<WorkflowAbout detail={detail} />);

    expect(screen.getByText(/This comes from/)).toBeInTheDocument();
    expect(screen.getByText(/which this app cannot run/)).toBeInTheDocument();
  });

  it("says a run hands a parameter straight back when the output node is a param", () => {
    const base = detailFixture();
    const detail = {
      ...base,
      graph: {
        ...base.graph,
        nodes: base.graph.nodes.map((n) =>
          n.kind === "param"
            ? { ...n, is_output: true, output_schema: { type: "string" } }
            : { ...n, is_output: false },
        ),
      },
    };
    render(<WorkflowAbout detail={detail} />);

    expect(screen.getByText("What a run hands back")).toBeInTheDocument();
    expect(screen.getByText(/, unchanged/)).toHaveTextContent(
      "the parameter collection, unchanged",
    );
    expect(screen.getByText("A string")).toBeInTheDocument();
  });
});

describe("WorkflowAbout, the author's own words", () => {
  it("shows the four sentences the author wrote, under their own headings", async () => {
    render(
      <WorkflowAbout
        detail={detailFixture({
          presentation: {
            purpose: "Prepare repeatable releases.",
            produces: "A reviewed rollout plan.",
            owner_participation: "Approve the rollout gate.",
            not_for: "Incidents already in progress.",
            inputs: [],
            result_fields: [],
            actions: [],
          },
        })}
      />,
    );

    expect(await screen.findByText("Prepare repeatable releases.")).toBeInTheDocument();
    expect(screen.getByText("A reviewed rollout plan.")).toBeInTheDocument();
    expect(screen.getByText("Approve the rollout gate.")).toBeInTheDocument();
    expect(screen.getByText("Incidents already in progress.")).toBeInTheDocument();
  });

  it("names what a workflow nobody described is still missing", async () => {
    render(
      <WorkflowAbout
        detail={detailFixture({
          readiness: {
            executable: true,
            described: false,
            findings: [
              {
                field: "produces",
                severity: "error",
                message: "Say what a finished run leaves the person holding.",
              },
            ],
          },
        })}
      />,
    );

    expect(
      await screen.findByText("Say what a finished run leaves the person holding."),
    ).toBeInTheDocument();
  });

  it("invents no words for a workflow whose library kept none", async () => {
    render(<WorkflowAbout detail={detailFixture()} />);

    await screen.findByText("What it does");
    expect(screen.queryByText("What it is for")).not.toBeInTheDocument();
  });
});

describe("the card editor writes the author's words", () => {
  it("offers a box for each of the four sentences, filled with what the author wrote", async () => {
    render(
      <WorkflowAbout
        detail={detailFixture({
          presentation: {
            purpose: "Prepare repeatable releases.",
            produces: "A reviewed rollout plan.",
            owner_participation: "Approve the rollout gate.",
            not_for: "Incidents already in progress.",
            inputs: [],
            result_fields: [],
            actions: [],
          },
        })}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));

    expect(screen.getByRole("textbox", { name: "What it is for" })).toHaveValue(
      "Prepare repeatable releases.",
    );
    expect(screen.getByRole("textbox", { name: "What a run hands you" })).toHaveValue(
      "A reviewed rollout plan.",
    );
    expect(screen.getByRole("textbox", { name: "What you do during a run" })).toHaveValue(
      "Approve the rollout gate.",
    );
    expect(screen.getByRole("textbox", { name: "What it is not for" })).toHaveValue(
      "Incidents already in progress.",
    );
  });

  it("sends the four sentences with the card, and closes the form on the answer", async () => {
    let patched: unknown = null;
    server.use(
      http.patch(`${BASE}/api/workflow/wf.research-digest.v1/card`, async ({ request }) => {
        patched = await request.json();
        return HttpResponse.json({
          ok: true,
          id: "wf.research-digest.v1",
          title: "Research digest",
          summary: "Turns undigested sources into written notes.",
          applicability: "Use nightly. Not for one source.",
          provisional: true,
          presentation: {
            purpose: "Keep the field's notes current.",
            produces: "A note per new source.",
            owner_participation: "You skim the notes.",
            not_for: "A single source you already read.",
            inputs: [],
            result_fields: [],
            actions: [],
          },
          readiness: { executable: true, described: true, findings: [] },
        });
      }),
    );
    render(<WorkflowAbout detail={detailFixture()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "What it is for" }),
      "Keep the field's notes current.",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "What a run hands you" }),
      "A note per new source.",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "What you do during a run" }),
      "You skim the notes.",
    );
    await userEvent.type(
      screen.getByRole("textbox", { name: "What it is not for" }),
      "A single source you already read.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(patched).toEqual({
        title: "Research digest",
        summary: "Turns undigested sources into written notes.",
        applicability:
          "Use for a field with a live knowledge base. Do not use for a one off reading list.",
        presentation: {
          purpose: "Keep the field's notes current.",
          produces: "A note per new source.",
          owner_participation: "You skim the notes.",
          not_for: "A single source you already read.",
        },
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument(),
    );
  });

  it("keeps the refusal's findings beside the boxes they name", async () => {
    server.use(
      http.patch(`${BASE}/api/workflow/wf.research-digest.v1/card`, () =>
        HttpResponse.json(
          {
            ok: false,
            message: "This workflow is not described yet.",
            readiness: {
              executable: true,
              described: false,
              findings: [
                {
                  field: "produces",
                  severity: "error",
                  message:
                    "This is a placeholder. Say what a finished run leaves the person holding.",
                },
              ],
            },
          },
          { status: 400 },
        ),
      ),
    );
    render(<WorkflowAbout detail={detailFixture()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByRole("textbox", { name: "What a run hands you" }), "TBD");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText(
        "This is a placeholder. Say what a finished run leaves the person holding.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });
});
