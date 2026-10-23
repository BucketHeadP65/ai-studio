import { useQuery } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { studioApi } from "@/api/studio";
import type { WorkflowDetail } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { WorkflowHeader } from "./workflow-header";

// The page's head: title, id@version, trust and Approve, and the prose -- identity only, never
// what acts on the run, which lives in its own card in the side column. Mounted here through a
// tiny harness that reads the same query key the real page's own `useQuery` does, so a save or a
// approval that lands through `queryClient.setQueryData` is seen on screen exactly the way it
// would be there, not just posted and forgotten.

const BASE = "http://studio.test";

function detailFixture(overrides: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    ok: true,
    id: "wf.lit-check.v5",
    name: "lit-check",
    version: 5,
    description: "Check a decision against the literature.",
    title: "Check against the literature",
    summary: "Checks one decision against the literature.",
    applicability: "Use before a decision ships. Do not use for a broad survey.",
    provisional: false,
    graph: {
      name: "lit-check",
      version: 5,
      nodes: [
        {
          key: "param:context",
          id: "context",
          kind: "param",
          signature: "string",
          detail: "what the decision applies to",
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
        {
          key: "draft",
          id: "draft",
          kind: "run",
          signature: "compose(context: string) → Record",
          detail: "",
          tools: [],
          required: true,
          is_output: true,
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
    spec_yaml: "name: lit-check\n",
    staleness: [],
    lineage: null,
    birth: [],
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
    ...overrides,
  };
}

// Reads the workflow off the same query key the real page's own `useQuery` populates, seeded
// with `initial` -- so a `setQueryData` the header's own mutations perform is what this test
// sees rendered, the same way a save or an approval reaches the real page.
function Harness({ initial }: { initial: WorkflowDetail }) {
  const query = useQuery({
    queryKey: ["studio", "workflow", initial.id],
    queryFn: () => studioApi.workflow(initial.id),
    initialData: initial,
  });
  return <WorkflowHeader detail={query.data} />;
}

describe("WorkflowHeader", () => {
  it("renders the identity a fixture carries: title, id@version, summary, applicability", async () => {
    const detail = detailFixture();
    render(<Harness initial={detail} />);

    expect(
      await screen.findByRole("heading", { name: "Check against the literature" }),
    ).toBeInTheDocument();
    expect(screen.getByText("wf.lit-check.v5 (v5)")).toBeInTheDocument();
    expect(screen.getByText("Checks one decision against the literature.")).toBeInTheDocument();
    const applicability = screen.getByText(
      "Use before a decision ships. Do not use for a broad survey.",
    );
    expect(applicability).toHaveAttribute("title", detail.applicability);
  });

  it("links to the what it does page next to Edit", async () => {
    render(<Harness initial={detailFixture()} />);

    expect(await screen.findByRole("link", { name: "What it does" })).toHaveAttribute(
      "href",
      "/workflows/library/wf.lit-check.v5",
    );
  });

  it("falls back to the id for the title when the card carries none", async () => {
    render(<Harness initial={detailFixture({ title: "" })} />);

    expect(await screen.findByRole("heading", { name: "wf.lit-check.v5" })).toBeInTheDocument();
  });

  it("edits round-trip through the PATCH route and land on screen", async () => {
    let patched: unknown = null;
    server.use(
      http.patch(`${BASE}/api/workflow/wf.lit-check.v5/card`, async ({ request }) => {
        patched = await request.json();
        return HttpResponse.json({
          archived_at: null,
          ok: true,
          id: "wf.lit-check.v5",
          title: "Check the literature first",
          summary: "New summary.",
          applicability: "New applicability.",
          provisional: false,
        });
      }),
    );
    render(<Harness initial={detailFixture()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const titleBox = screen.getByRole("textbox", { name: "Title" });
    await userEvent.clear(titleBox);
    await userEvent.type(titleBox, "Check the literature first");
    const summaryBox = screen.getByRole("textbox", { name: "Summary" });
    await userEvent.clear(summaryBox);
    await userEvent.type(summaryBox, "New summary.");
    const applicabilityBox = screen.getByRole("textbox", { name: "Applicability" });
    await userEvent.clear(applicabilityBox);
    await userEvent.type(applicabilityBox, "New applicability.");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(patched).toEqual({
        title: "Check the literature first",
        summary: "New summary.",
        applicability: "New applicability.",
      }),
    );
    expect(
      await screen.findByRole("heading", { name: "Check the literature first" }),
    ).toBeInTheDocument();
    expect(screen.getByText("New summary.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });

  it("an empty title shows the backend's own sentence, and stays in edit", async () => {
    server.use(
      http.patch(`${BASE}/api/workflow/wf.lit-check.v5/card`, () =>
        HttpResponse.json(
          { ok: false, message: "a workflow needs a title. it cannot be blank." },
          { status: 400 },
        ),
      ),
    );
    render(<Harness initial={detailFixture()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.clear(screen.getByRole("textbox", { name: "Title" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("a workflow needs a title. it cannot be blank."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("cancelling an edit discards it and restores the card's own words", async () => {
    render(<Harness initial={detailFixture()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.clear(screen.getByRole("textbox", { name: "Title" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Title" }), "Abandoned edit");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(
      await screen.findByRole("heading", { name: "Check against the literature" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Abandoned edit")).not.toBeInTheDocument();
  });

  it("approves from the header and shows the refreshed level", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/workflows/wf.lit-check.v5/approve`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({
          ok: true,
          trust: {
            level: "approved",
            runs_with_evidence: 2,
            failed_runs: 0,
            approved_by: ["owner"],
            structural_grades: 0,
            structural_mean: null,
            structural_last: null,
          },
        });
      }),
    );
    render(<Harness initial={detailFixture()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Approve it" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Your name" }), "owner");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(posted).toEqual({ by: "owner", note: "" }));
    expect(await screen.findByText("approved")).toBeInTheDocument();
  });

  it("shows the archived badge and Restore for an archived detail, never Archive", async () => {
    render(<Harness initial={detailFixture({ archived_at: "2026-08-01T00:00:00Z" })} />);

    expect(await screen.findByText("archived")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });

  it("shows Archive for a live detail, never the archived badge or Restore", async () => {
    render(<Harness initial={detailFixture()} />);

    expect(await screen.findByRole("button", { name: "Archive" })).toBeInTheDocument();
    expect(screen.queryByText("archived")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore" })).not.toBeInTheDocument();
  });

  it("archives from the header, posting to the archive route and showing the archived badge", async () => {
    let archivePosted = false;
    server.use(
      http.post(`${BASE}/api/workflow/wf.lit-check.v5/archive`, () => {
        archivePosted = true;
        return HttpResponse.json({
          ok: true,
          id: "wf.lit-check.v5",
          title: "Check against the literature",
          summary: "Checks one decision against the literature.",
          applicability: "Use before a decision ships. Do not use for a broad survey.",
          provisional: false,
          archived_at: "2026-08-24T10:00:00Z",
        });
      }),
    );
    render(<Harness initial={detailFixture()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Archive" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));

    await waitFor(() => expect(archivePosted).toBe(true));
    expect(await screen.findByText("archived")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
  });

  it("restores from the header, posting to the restore route and showing Archive again", async () => {
    let restorePosted = false;
    server.use(
      http.post(`${BASE}/api/workflow/wf.lit-check.v5/restore`, () => {
        restorePosted = true;
        return HttpResponse.json({
          ok: true,
          id: "wf.lit-check.v5",
          title: "Check against the literature",
          summary: "Checks one decision against the literature.",
          applicability: "Use before a decision ships. Do not use for a broad survey.",
          provisional: false,
          archived_at: null,
        });
      }),
    );
    render(<Harness initial={detailFixture({ archived_at: "2026-08-01T00:00:00Z" })} />);

    await userEvent.click(await screen.findByRole("button", { name: "Restore" }));

    await waitFor(() => expect(restorePosted).toBe(true));
    expect(await screen.findByRole("button", { name: "Archive" })).toBeInTheDocument();
    expect(screen.queryByText("archived")).not.toBeInTheDocument();
  });
});

describe("WorkflowHeader readiness", () => {
  it("shows what this card still lacks, beside the boxes that own it", async () => {
    render(
      <Harness
        initial={detailFixture({
          readiness: {
            executable: true,
            described: false,
            findings: [
              {
                field: "title",
                severity: "error",
                message: "A title says what this workflow is called. Write one.",
              },
              {
                field: "purpose",
                severity: "error",
                message: "Say what this workflow is for.",
              },
            ],
          },
        })}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));

    expect(
      screen.getByText("A title says what this workflow is called. Write one."),
    ).toBeInTheDocument();
    expect(screen.getByText("Say what this workflow is for.")).toBeInTheDocument();
  });

  it("sends all three card fields, and keeps the backend's reading after a refusal", async () => {
    let patched: unknown = null;
    server.use(
      http.patch(`${BASE}/api/workflow/wf.lit-check.v5/card`, async ({ request }) => {
        patched = await request.json();
        return HttpResponse.json(
          {
            ok: false,
            message: "This workflow is not described yet.",
            readiness: {
              executable: true,
              described: false,
              findings: [
                {
                  field: "title",
                  severity: "error",
                  message: "This title is a placeholder. Write the real one.",
                },
              ],
            },
          },
          { status: 400 },
        );
      }),
    );
    render(<Harness initial={detailFixture()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const titleBox = screen.getByRole("textbox", { name: "Title" });
    await userEvent.clear(titleBox);
    await userEvent.type(titleBox, "TBD");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("This title is a placeholder. Write the real one."),
    ).toBeInTheDocument();
    expect(screen.getByText("This workflow is not described yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    await waitFor(() =>
      expect(patched).toEqual({
        title: "TBD",
        summary: "Checks one decision against the literature.",
        applicability: "Use before a decision ships. Do not use for a broad survey.",
      }),
    );
  });
});
