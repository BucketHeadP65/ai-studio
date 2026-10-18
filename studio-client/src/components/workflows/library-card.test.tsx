import { screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunView } from "@/api/sessions-types";
import type { LibraryEntry, WorkflowDetail } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the card sees the mocked module.
import { LibraryCard, type LibraryCardProps } from "./library-card";

// One card's own anatomy: the fact row it draws off `shape`, the one status sentence its
// footer picks off live runs and the last finished one, the guarded whole card click, the
// archived variant's swap to Restore, and the setup Run opens before anything starts.

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
    shape: { steps: 4, person_gates: 1, agent_gates: 2 },
    runs: { last: null },
    spend: { cost_usd: "0", unpriced: 0, runs: 0 },
    ...overrides,
  };
}

function runFixture(overrides: Partial<RunView> = {}): RunView {
  return {
    session_id: "sess-1",
    workflow_id: "wf.sales.v1",
    run_title: "",
    state: "waiting",
    active: [],
    done: [],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: "2026-08-24T10:00:00Z",
    spend: null,
    ...overrides,
  };
}

function renderCard(overrides: Partial<LibraryCardProps> = {}) {
  const props: LibraryCardProps = {
    card: cardFixture(),
    liveRuns: [],
    archived: false,
    onRestore: vi.fn(),
    restoring: false,
    ...overrides,
  };
  render(<LibraryCard {...props} />);
  return props;
}

function toneOf(text: string): string | undefined {
  return screen.getByText(text).parentElement?.className;
}

describe("LibraryCard", () => {
  afterEach(() => {
    routerPush.mockClear();
  });

  it("shows the name, version tag, and summary", () => {
    renderCard();

    expect(screen.getByRole("link", { name: "Fetch sales" })).toHaveAttribute(
      "href",
      "/workflows/library/wf.sales.v1",
    );
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("Pull last quarter's sales into one report.")).toBeInTheDocument();
  });

  it("reads the version off the id's own suffix when the library carries none", () => {
    renderCard({ card: cardFixture({ version: null, id: "wf.sales.v3" }) });

    expect(screen.getByText("v3")).toBeInTheDocument();
  });

  describe("the fact row", () => {
    it("shows steps and both gate kinds, pluralized", () => {
      renderCard({ card: cardFixture({ shape: { steps: 4, person_gates: 2, agent_gates: 3 } }) });

      expect(screen.getByText("4 steps")).toBeInTheDocument();
      expect(screen.getByText("2 person gates")).toBeInTheDocument();
      expect(screen.getByText("3 agent gates")).toBeInTheDocument();
    });

    it("uses the singular for a count of one", () => {
      renderCard({ card: cardFixture({ shape: { steps: 1, person_gates: 1, agent_gates: 1 } }) });

      expect(screen.getByText("1 step")).toBeInTheDocument();
      expect(screen.getByText("1 person gate")).toBeInTheDocument();
      expect(screen.getByText("1 agent gate")).toBeInTheDocument();
    });

    it("names no gates when neither gate count is set, but still shows steps", () => {
      renderCard({ card: cardFixture({ shape: { steps: 3, person_gates: 0, agent_gates: 0 } }) });

      expect(screen.getByText("3 steps")).toBeInTheDocument();
      expect(screen.getByText("no gates")).toBeInTheDocument();
      expect(screen.queryByText(/person gate/)).not.toBeInTheDocument();
      expect(screen.queryByText(/agent gate/)).not.toBeInTheDocument();
    });

    it("shows the running spend, and how many runs it covers, once one has been priced", () => {
      renderCard({ card: cardFixture({ spend: { cost_usd: "1.23", unpriced: 0, runs: 3 } }) });

      expect(screen.getByText("$1.23 over 3 runs")).toBeInTheDocument();
    });

    it("counts a single run in the singular", () => {
      renderCard({ card: cardFixture({ spend: { cost_usd: "1.23", unpriced: 0, runs: 1 } }) });

      expect(screen.getByText("$1.23 over 1 run")).toBeInTheDocument();
    });

    it("shows no spend fact when no run has been priced", () => {
      renderCard({ card: cardFixture({ spend: { cost_usd: "0", unpriced: 0, runs: 0 } }) });

      expect(screen.queryByText(/^\$/)).not.toBeInTheDocument();
    });
  });

  describe("the footer's one status sentence", () => {
    beforeEach(() => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-08-25T10:00:00Z"));
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("says runs are waiting on you, ahead of everything else, singular for one", () => {
      renderCard({
        card: cardFixture({
          runs: {
            last: { outcome: "failed", started_at: "", ended_at: "2026-08-25T08:00:00Z" },
          },
        }),
        liveRuns: [runFixture({ state: "waiting" })],
      });

      expect(screen.getByText("1 run waiting on you.")).toBeInTheDocument();
      expect(toneOf("1 run waiting on you.")).toContain("text-warn-ink");
    });

    it("pluralizes multiple runs waiting", () => {
      renderCard({
        liveRuns: [
          runFixture({ session_id: "sess-1", state: "waiting" }),
          runFixture({ session_id: "sess-2", state: "waiting" }),
        ],
      });

      expect(screen.getByText("2 runs waiting on you.")).toBeInTheDocument();
    });

    it("says runs are in flight when running or paused, outranking the last finished run", () => {
      renderCard({
        card: cardFixture({
          runs: {
            last: { outcome: "completed", started_at: "", ended_at: "2026-08-25T08:00:00Z" },
          },
        }),
        liveRuns: [runFixture({ state: "paused" })],
      });

      expect(screen.getByText("1 run in flight.")).toBeInTheDocument();
      expect(toneOf("1 run in flight.")).toContain("text-accent-ink");
    });

    it("says the last run failed, relative to when it ended, for a failed or a stopped outcome", () => {
      renderCard({
        card: cardFixture({
          runs: {
            last: { outcome: "stopped", started_at: "", ended_at: "2026-08-25T08:00:00Z" },
          },
        }),
      });

      expect(screen.getByText("Last run failed 2 hours ago.")).toBeInTheDocument();
      expect(toneOf("Last run failed 2 hours ago.")).toContain("text-warn-ink");
    });

    it("says the last run completed, relative to when it ended", () => {
      renderCard({
        card: cardFixture({
          runs: {
            last: { outcome: "completed", started_at: "", ended_at: "2026-08-25T09:30:00Z" },
          },
        }),
      });

      expect(screen.getByText("Last run completed 30 minutes ago.")).toBeInTheDocument();
      expect(toneOf("Last run completed 30 minutes ago.")).toContain("text-label-2");
    });

    it("says never run when the workflow has no run on record", () => {
      renderCard({ card: cardFixture({ runs: { last: null } }) });

      expect(screen.getByText("Never run.")).toBeInTheDocument();
      expect(toneOf("Never run.")).toContain("text-label-3");
    });

    it("never truncates the sentence, letting it wrap instead of clipping mid word", () => {
      renderCard({
        card: cardFixture({
          runs: {
            last: { outcome: "completed", started_at: "", ended_at: "2026-08-25T07:00:00Z" },
          },
        }),
      });

      const sentence = screen.getByText("Last run completed 3 hours ago.");
      expect(sentence.className).not.toContain("truncate");
    });
  });

  it("opens the workflow on a card body click, while the trust chip never also navigates", async () => {
    renderCard();

    await userEvent.click(screen.getByText("Pull last quarter's sales into one report."));
    expect(routerPush).toHaveBeenCalledWith("/workflows/library/wf.sales.v1");

    routerPush.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Approve it" }));
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("shows Restore instead of Run for an archived card, and guards its click too", async () => {
    const onRestore = vi.fn();
    renderCard({
      archived: true,
      onRestore,
      card: cardFixture({ archived_at: "2026-08-01T00:00:00Z" }),
    });

    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(onRestore).toHaveBeenCalledWith("wf.sales.v1");
    expect(routerPush).not.toHaveBeenCalled();
  });

  describe("the disabled-run reason", () => {
    it("says the spec could not be read, ahead of everything else, disabling Run", () => {
      renderCard({ card: cardFixture({ missing: null }) });

      expect(screen.getByText("Cannot run, the spec could not be read.")).toBeInTheDocument();
      expect(toneOf("Cannot run, the spec could not be read.")).toContain("text-warn-ink");
      const runButton = screen.getByRole("button", { name: "Run" });
      expect(runButton).toBeDisabled();
      expect(runButton).toHaveAttribute("title", "Cannot run, the spec could not be read.");
    });

    it("names how many steps are missing, plural", () => {
      renderCard({ card: cardFixture({ missing: ["step-a", "step-b"] }) });

      expect(screen.getByText("Cannot run, 2 steps are missing.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Run" })).toHaveAttribute(
        "title",
        "Cannot run, 2 steps are missing.",
      );
    });

    it("uses the singular for one missing step, the same words in the sentence and the tooltip", () => {
      renderCard({ card: cardFixture({ missing: ["step-a"] }) });

      const runButton = screen.getByRole("button", { name: "Run" });
      expect(screen.getByText("Cannot run, 1 step is missing.")).toBeInTheDocument();
      expect(runButton).toBeDisabled();
      expect(runButton).toHaveAttribute("title", "Cannot run, 1 step is missing.");
    });

    it("outranks runs waiting on you, since a workflow that cannot run has nothing to wait on", () => {
      renderCard({
        card: cardFixture({ missing: ["step-a"] }),
        liveRuns: [runFixture({ state: "waiting" })],
      });

      expect(screen.getByText("Cannot run, 1 step is missing.")).toBeInTheDocument();
      expect(screen.queryByText(/waiting on you/)).not.toBeInTheDocument();
    });

    it("never shows on an archived card, which has no Run button to explain", () => {
      renderCard({ archived: true, card: cardFixture({ missing: ["step-a"] }) });

      expect(screen.queryByText(/Cannot run/)).not.toBeInTheDocument();
    });
  });

  describe("the setup before a run", () => {
    function detailFixture(): WorkflowDetail {
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
              required: true,
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

    function mountWorkflow(): { creates: number; runs: unknown[] } {
      const wire = { creates: 0, runs: [] as unknown[] };
      server.use(
        http.get(`${BASE}/api/workflow/wf.sales.v1`, () => HttpResponse.json(detailFixture())),
        http.post(`${BASE}/api/sessions`, () => {
          wire.creates += 1;
          return HttpResponse.json({
            session: {
              id: "sess-new",
              project_id: "proj-1",
              title: "session sess-new",
              started_at: "2026-09-13T10:00:00Z",
              last_activity_at: "2026-09-13T10:00:00Z",
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
        http.patch(`${BASE}/api/sessions/:id`, async ({ request }) => {
          const body = (await request.json()) as { title: string };
          return HttpResponse.json({
            session: {
              id: "sess-new",
              project_id: "proj-1",
              title: body.title,
              started_at: "2026-09-13T10:00:00Z",
              last_activity_at: "2026-09-13T10:00:00Z",
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
        http.post(`${BASE}/api/sessions/:id/run`, async ({ request }) => {
          wire.runs.push(await request.json());
          return HttpResponse.json({ ok: true, running: "wf.sales.v1" });
        }),
      );
      return wire;
    }

    it("opens setup on Run rather than starting a run with no arguments", async () => {
      const wire = mountWorkflow();
      renderCard();

      await userEvent.click(screen.getByRole("button", { name: "Run" }));

      const dialog = await screen.findByRole("dialog");
      expect(await within(dialog).findByLabelText("quarter")).toBeInTheDocument();
      expect(wire.creates).toBe(0);
      expect(wire.runs).toEqual([]);
      expect(routerPush).not.toHaveBeenCalled();
    });

    it("opens a fresh setup after a refusal, rather than greeting it with the old one", async () => {
      mountWorkflow();
      server.use(
        http.post(`${BASE}/api/sessions`, () =>
          HttpResponse.json({ detail: "wf.sales.v1 is still running" }, { status: 409 }),
        ),
      );
      renderCard();

      await userEvent.click(screen.getByRole("button", { name: "Run" }));
      const dialog = await screen.findByRole("dialog");
      await userEvent.type(await within(dialog).findByLabelText("quarter"), "Q1");
      await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent(
        "wf.sales.v1 is still running",
      );

      await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      await userEvent.click(screen.getByRole("button", { name: "Run" }));

      const reopened = await screen.findByRole("dialog");
      expect(within(reopened).queryByRole("alert")).not.toBeInTheDocument();
    });

    it("starts the run with what setup collected, then opens its inspector", async () => {
      const wire = mountWorkflow();
      renderCard();

      await userEvent.click(screen.getByRole("button", { name: "Run" }));
      const dialog = await screen.findByRole("dialog");
      await userEvent.type(await within(dialog).findByLabelText("quarter"), "Q1");
      await userEvent.click(within(dialog).getByRole("button", { name: "Run it" }));

      await waitFor(() =>
        expect(wire.runs).toEqual([
          { workflow_id: "wf.sales.v1", arguments: { quarter: "Q1" }, run_title: "Fetch sales" },
        ]),
      );
      expect(wire.creates).toBe(1);
      await waitFor(() => expect(routerPush).toHaveBeenCalledWith("/workflows/wf.sales.v1"));
    });
  });
});
