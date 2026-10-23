import { screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { ReactElement } from "react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { launchWorkflowRun } from "@/api/sessions";
import { studioErrorMessage } from "@/api/studio";
import type { RunIntent, WorkflowDetail } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { BeforeRunDialog } from "./before-run-dialog";

// Nothing reaches the backend until this dialog resolves an intent: the run's own title, its
// typed arguments, and who decides its gates. Cancelling, pressing Escape, or confirming while a
// field is wrong all leave the runner untouched, which the counted POSTs below are the proof of.

const STUDIO = "http://studio.test";
const KNOWLEDGE = "http://api.test";

function detailFixture(overrides: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    ok: true,
    id: "wf.decide.v2",
    name: "decide",
    version: 2,
    description: "Check a decision against the literature.",
    title: "Decide with the literature",
    summary: "Answers one decision from the knowledge bases you pick.",
    applicability: "",
    provisional: false,
    graph: {
      name: "decide",
      version: 2,
      nodes: [
        {
          key: "param:question",
          id: "question",
          kind: "param",
          signature: "string",
          detail: "",
          tools: [],
          required: true,
          is_output: false,
          unknown: false,
          brought: false,
          briefing: "",
          input_schemas: {},
          output_schema: { type: "string", minLength: 1 },
          source: "",
        },
        {
          key: "param:knowledge_bases",
          id: "knowledge_bases",
          kind: "param",
          signature: "array",
          detail: "",
          tools: [],
          required: true,
          is_output: false,
          unknown: false,
          brought: false,
          briefing: "",
          input_schemas: {},
          output_schema: {
            type: "array",
            items: { type: "string" },
            minItems: 1,
            maxItems: 3,
            uniqueItems: true,
          },
          source: "",
        },
        {
          key: "param:publish",
          id: "publish",
          kind: "param",
          signature: "boolean",
          required: false,
          detail: "",
          tools: [],
          is_output: false,
          unknown: false,
          brought: false,
          briefing: "",
          input_schemas: {},
          output_schema: { type: "boolean" },
          source: "",
        },
        {
          key: "review",
          id: "review",
          kind: "gate",
          signature: "gate",
          detail: "",
          tools: [],
          required: true,
          is_output: false,
          unknown: false,
          brought: false,
          briefing: "",
          input_schemas: {},
          output_schema: null,
          source: "",
        },
      ],
      edges: [],
    },
    issues: [],
    fits: true,
    missing: [],
    brings: [],
    spec_yaml: "name: decide\n",
    staleness: [],
    lineage: null,
    birth: null,
    carried: null,
    predictions: null,
    prediction_record: null,
    trust: {
      level: "machine-confirmed",
      runs_with_evidence: 2,
      failed_runs: 0,
      approved_by: [],
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
    },
    archived_at: null,
    presentation: {
      purpose: "",
      produces: "",
      owner_participation: "",
      not_for: "",
      inputs: [
        { key: "question", label: "What are you deciding?", control: "textarea" },
        { key: "knowledge_bases", label: "Knowledge bases", control: "knowledge-base" },
      ],
      result_fields: [],
      actions: [],
    },
    ...overrides,
  };
}

function sessionFixture(title: string) {
  return {
    id: "sess-new",
    project_id: "proj-1",
    title,
    started_at: "2026-09-13T10:00:00Z",
    last_activity_at: "2026-09-13T10:00:00Z",
    archived: false,
    answering: false,
    turn_count: 0,
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 },
    kind: "runner",
    cost_usd: "0",
    unpriced: 0,
    context: null,
  };
}

interface Wire {
  creates: number;
  renamed: string[];
  runs: unknown[];
  knowledgeBaseReads: number;
  intents: RunIntent[];
}

function mountApi(options: { runStatus?: number; runDetail?: string } = {}): Wire {
  const wire: Wire = { creates: 0, renamed: [], runs: [], knowledgeBaseReads: 0, intents: [] };
  server.use(
    http.get(`${KNOWLEDGE}/api/knowledge-bases`, () => {
      wire.knowledgeBaseReads += 1;
      return HttpResponse.json({
        knowledge_bases: ["research", "repo-docs", "vault"].map((name) => ({
          name,
          registered: true,
          indexed: true,
          broken: false,
          path: `/kb/${name}`,
          description: "",
          pages: 1,
          page_types: [],
          last_indexed: null,
        })),
      });
    }),
    http.post(`${STUDIO}/api/sessions`, () => {
      wire.creates += 1;
      return HttpResponse.json({ session: sessionFixture("session sess-new") });
    }),
    http.patch(`${STUDIO}/api/sessions/:id`, async ({ request }) => {
      const body = (await request.json()) as { title: string };
      wire.renamed.push(body.title);
      return HttpResponse.json({ session: sessionFixture(body.title) });
    }),
    http.post(`${STUDIO}/api/sessions/:id/run`, async ({ request }) => {
      wire.runs.push(await request.json());
      if (options.runStatus !== undefined) {
        return HttpResponse.json(
          { detail: options.runDetail ?? "refused" },
          { status: options.runStatus },
        );
      }
      return HttpResponse.json({ ok: true, running: "wf.decide.v2" });
    }),
  );
  return wire;
}

// The opener a real screen puts in front of this dialog: a Run button, the launch it owns, and
// the refusal it hands back for the dialog to keep showing.
function Opener({ wire, detail }: { wire: Wire; detail?: WorkflowDetail | null }): ReactElement {
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Run
      </button>
      <BeforeRunDialog
        open={open}
        workflowId="wf.decide.v2"
        workflowTitle="Decide with the literature"
        detail={detail === undefined ? detailFixture() : detail}
        submitting={submitting}
        error={error}
        onConfirm={async (intent) => {
          wire.intents.push(intent);
          setSubmitting(true);
          setError(null);
          try {
            await launchWorkflowRun(intent, "Decide with the literature");
            setOpen(false);
          } catch (failure) {
            setError(studioErrorMessage(failure));
          } finally {
            setSubmitting(false);
          }
        }}
        onCancel={() => setOpen(false)}
      />
    </>
  );
}

async function openDialog(wire: Wire, detail?: WorkflowDetail | null): Promise<HTMLElement> {
  render(<Opener wire={wire} detail={detail} />);
  await userEvent.click(screen.getByRole("button", { name: "Run" }));
  return await screen.findByRole("dialog");
}

async function fillValidly(dialog: HTMLElement): Promise<void> {
  await userEvent.type(
    within(dialog).getByLabelText("What are you deciding?"),
    "Which cache do we use?",
  );
  await userEvent.click(await within(dialog).findByRole("checkbox", { name: "research" }));
}

describe("BeforeRunDialog", () => {
  it("moves focus into itself when the opener opens it", async () => {
    const wire = mountApi();
    const dialog = await openDialog(wire);

    await waitFor(() => {
      const focused = document.activeElement;
      expect(focused instanceof HTMLElement && dialog.contains(focused)).toBe(true);
    });
  });

  it("returns focus to the opener on Escape, and starts nothing", async () => {
    const wire = mountApi();
    const dialog = await openDialog(wire);
    await fillValidly(dialog);

    await userEvent.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Run" })).toHaveFocus();
    expect(wire.creates).toBe(0);
    expect(wire.runs).toEqual([]);
  });

  it("returns focus to the opener on Cancel, and starts nothing", async () => {
    const wire = mountApi();
    const dialog = await openDialog(wire);
    await fillValidly(dialog);

    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Run" })).toHaveFocus();
    expect(wire.creates).toBe(0);
    expect(wire.runs).toEqual([]);
  });

  it("starts nothing when a field is wrong, saying which one instead", async () => {
    const wire = mountApi();
    const dialog = await openDialog(wire);

    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    expect(within(dialog).getAllByText("This is required.")).toHaveLength(2);
    expect(wire.intents).toEqual([]);
    expect(wire.creates).toBe(0);
    expect(wire.runs).toEqual([]);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("says how many fields are still unanswered when a confirm is refused here", async () => {
    const wire = mountApi();
    const dialog = await openDialog(wire);

    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("2 fields need an answer.");

    await userEvent.type(
      within(dialog).getByLabelText("What are you deciding?"),
      "Which cache do we use?",
    );
    expect(within(dialog).getByRole("alert")).toHaveTextContent("1 field needs an answer.");
  });

  it("resolves one intent, then creates and runs exactly once", async () => {
    const wire = mountApi();
    const dialog = await openDialog(wire);
    await fillValidly(dialog);

    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    await waitFor(() => expect(wire.runs).toHaveLength(1));
    expect(wire.intents).toEqual([
      {
        workflowId: "wf.decide.v2",
        runTitle: "Decide with the literature, Which cache do we use?",
        arguments: {
          question: "Which cache do we use?",
          knowledge_bases: ["research"],
          publish: false,
        },
        gateMode: "wait",
      },
    ]);
    // Arrays stay arrays and booleans stay booleans on the wire, never their printed form.
    expect(wire.runs[0]).toEqual({
      workflow_id: "wf.decide.v2",
      arguments: {
        question: "Which cache do we use?",
        knowledge_bases: ["research"],
        publish: false,
      },
      run_title: "Decide with the literature, Which cache do we use?",
    });
    expect(wire.creates).toBe(1);
    // The session is the container the run lives in, so it carries the workflow's own name.
    expect(wire.renamed).toEqual(["Decide with the literature"]);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("keeps a backend refusal on screen with everything typed still there", async () => {
    const wire = mountApi({ runStatus: 400, runDetail: "question: '' is too short" });
    const dialog = await openDialog(wire);
    await fillValidly(dialog);

    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("question: '' is too short");
    expect(within(dialog).getByLabelText("What are you deciding?")).toHaveValue(
      "Which cache do we use?",
    );
    expect(within(dialog).getByRole("checkbox", { name: "research" })).toBeChecked();
  });

  it("names the run, defaulting from the card and the first input the author labelled", async () => {
    const wire = mountApi();
    const dialog = await openDialog(wire);
    await fillValidly(dialog);

    const title = within(dialog).getByLabelText("Run title");
    expect(title).toHaveValue("Decide with the literature, Which cache do we use?");

    await userEvent.clear(title);
    await userEvent.type(title, "Cache decision, round two");
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    await waitFor(() => expect(wire.intents).toHaveLength(1));
    expect(wire.intents[0]?.runTitle).toBe("Cache decision, round two");
  });

  it("asks who decides the gates only when this workflow has one", async () => {
    const wire = mountApi();
    const dialog = await openDialog(wire);

    expect(within(dialog).getByText("Gates on this run")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("radio", { name: "Pass automatically" }));
    await fillValidly(dialog);
    await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

    await waitFor(() => expect(wire.intents).toHaveLength(1));
    expect(wire.intents[0]?.gateMode).toBe("auto");
    expect(wire.runs[0]).toMatchObject({ gate_mode: "auto" });
  });

  it("leaves the gate question out for a workflow with no gate at all", async () => {
    const wire = mountApi();
    const gateless = detailFixture();
    const dialog = await openDialog(wire, {
      ...gateless,
      graph: {
        ...gateless.graph,
        nodes: gateless.graph.nodes.filter((node) => node.kind !== "gate"),
      },
    });

    expect(within(dialog).queryByText("Gates on this run")).not.toBeInTheDocument();
  });

  it("reads the registry only for a knowledge-base input", async () => {
    const wire = mountApi();
    const plain = detailFixture();
    const dialog = await openDialog(wire, {
      ...plain,
      graph: {
        ...plain.graph,
        nodes: plain.graph.nodes.filter((node) => node.id !== "knowledge_bases"),
      },
      presentation: {
        ...(plain.presentation ?? {
          purpose: "",
          produces: "",
          owner_participation: "",
          not_for: "",
          result_fields: [],
          actions: [],
        }),
        inputs: [{ key: "question", label: "What are you deciding?", control: "textarea" }],
      },
    });

    await within(dialog).findByLabelText("What are you deciding?");
    expect(wire.knowledgeBaseReads).toBe(0);
  });

  it("fetches the workflow itself when the opener holds no detail", async () => {
    const wire = mountApi();
    let fetched = 0;
    server.use(
      http.get(`${STUDIO}/api/workflow/:id`, () => {
        fetched += 1;
        return HttpResponse.json(detailFixture());
      }),
    );
    const dialog = await openDialog(wire, null);

    expect(await within(dialog).findByLabelText("What are you deciding?")).toBeInTheDocument();
    expect(fetched).toBe(1);
  });

  it("says its own words in the house's punctuation", async () => {
    const wire = mountApi();
    const dialog = await openDialog(wire);

    expect(dialog.textContent ?? "").toMatch(/^[\w\s,.?()'-]*$/);
  });
});
