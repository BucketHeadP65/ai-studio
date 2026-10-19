import { screen } from "@testing-library/dom";
import { describe, expect, it, vi } from "vitest";

import {
  LibraryEntrySchema,
  RunEnvelopeSchema,
  WorkflowDetailSchema,
  WorkflowPresentationSchema,
} from "@/api/studio-types";
import { render } from "@/test-support/render";

const routerPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: routerPush }),
}));

// Imported after the mock so the components see the mocked module.
import { ArtifactActions } from "./artifact-actions";
import { RESULT_STATUS, resultReading } from "./outcome-strip";
import { WorkflowAbout } from "./workflow-about";

// Payloads hand-typed rather than built from a current fixture, so a field that starts being sent
// by default fails here instead of the fixture quietly growing it. Each case is parsed at the
// boundary the app actually parses at, then put in front of the component that reads it.

const UNDESCRIBED_CARD = {
  id: "wf.quarterly-close.v1",
  title: "Quarterly close",
  summary: "Reconcile the quarter and draft the report.",
  provisional: false,
  trust: {
    level: "unverified",
    runs_with_evidence: 0,
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
  shape: { steps: 0, person_gates: 0, agent_gates: 0 },
  runs: { last: null },
};

const UNDESCRIBED_DETAIL = {
  ...UNDESCRIBED_CARD,
  ok: true as const,
  name: "quarterly-close",
  description: "Collect the quarter's sales and costs, reconcile them, and draft a report.",
  applicability: "Use this at the end of a fiscal quarter.",
  graph: { name: "quarterly-close", version: 1, nodes: [], edges: [] },
  issues: [],
  fits: true,
  spec_yaml: "name: quarterly-close\n",
  staleness: [],
  lineage: null,
  birth: [],
  carried: null,
  predictions: null,
  prediction_record: null,
};

const UNNAMED_RUN = {
  run_id: "run-unnamed",
  workflow_id: "wf.quarterly-close.v1",
  run_title: "",
  outcome: "completed" as const,
  failure: "",
  started_at: "2026-01-05T09:00:00+00:00",
  ended_at: "2026-01-05T09:04:00+00:00",
  runner: "app:harness",
  session_id: "sess-shared",
  params: {},
  gates_approved: [],
  gates_rejected: {},
  gates_auto: [],
  gates_orchestrated: [],
  pinned: false,
  spend: null,
  result: { status: "unknown" as const },
};

const SESSION_TITLE = "Tuesday morning, catching up on the quarter";

describe("a library card nobody described", () => {
  it("parses with the card's own words and no run behind it", () => {
    const card = LibraryEntrySchema.parse(UNDESCRIBED_CARD);

    expect(card.title).toBe("Quarterly close");
    expect(card.runs.last).toBeNull();
  });

  it("renders its own page with none of the four questions answered on its behalf", () => {
    const detail = WorkflowDetailSchema.parse(UNDESCRIBED_DETAIL);
    render(<WorkflowAbout detail={detail} />);

    expect(detail.presentation).toBeUndefined();
    for (const question of [
      "What it is for",
      "What a run hands you",
      "What you do during a run",
      "What it is not for",
    ]) {
      expect(screen.queryByText(question)).not.toBeInTheDocument();
    }
  });

  it("still shows what the spec itself says, which is the card's own words", () => {
    render(<WorkflowAbout detail={WorkflowDetailSchema.parse(UNDESCRIBED_DETAIL)} />);

    expect(screen.getByRole("heading", { level: 1, name: "Quarterly close" })).toBeInTheDocument();
    expect(screen.getByText("Reconcile the quarter and draft the report.")).toBeInTheDocument();
  });
});

describe("a run nobody named", () => {
  it("carries no name of the session it ran in, however that session was titled", () => {
    const run = RunEnvelopeSchema.parse(UNNAMED_RUN);

    expect(run.run_title).toBe("");
    expect(JSON.stringify(run)).not.toContain(SESSION_TITLE);
    expect(run.session_id).toBe("sess-shared");
  });
});

describe("the words an outcome is shown in", () => {
  // A checked outcome knows what the producer did. Whether the work was any good is the owner's
  // own record, kept as its own event, so no reading on this side may reach for these words.
  const claims = ["useful", "valuable", "worthwhile", "helpful", "worth doing", "quality"];

  it.each(["succeeded", "noop", "partial", "failed", "unknown"] as const)(
    "never claims %s was any good",
    (status) => {
      const reading = resultReading({
        status,
        summary: "",
        attempted: 2,
        succeeded: 1,
        skipped: 0,
        failed: 1,
        retry_attempts: 0,
        primary_input: 2,
        generation: 0,
        checks: [],
        artifacts: [],
      });
      const said = `${RESULT_STATUS[status].word} ${reading}`.toLowerCase();

      expect(claims.filter((claim) => said.includes(claim))).toEqual([]);
    },
  );
});

describe("an artifact action this deployment cannot carry out", () => {
  const ACTION = {
    kind: "save_note" as const,
    id: "keep",
    label: "Keep it as a note",
    artifact_id: "report",
    destination: "user_selects" as const,
  };
  const REF = {
    id: "report",
    label: "The report",
    run_id: "run-unnamed",
    node_id: "render",
    turn: null,
    value_digest: "a".repeat(64),
    media_type: "text/markdown" as const,
  };

  it("says so and does nothing, rather than reaching for a route of its own", () => {
    render(<ArtifactActions runId="run-unnamed" actions={[ACTION]} artifacts={[REF]} />);

    expect(screen.getByRole("button", { name: "Keep it as a note" })).toBeDisabled();
    expect(screen.getByText("This action is not available here yet.")).toBeInTheDocument();
  });

  it("refuses a stored action naming an intent nothing here implements", () => {
    const stored = {
      actions: [
        { kind: "publish_to_web", id: "publish", label: "Publish it", artifact_id: "report" },
      ],
    };

    expect(WorkflowPresentationSchema.safeParse(stored).success).toBe(false);
  });

  it("refuses a known intent that tries to carry a route with it", () => {
    const stored = { actions: [{ ...ACTION, endpoint: "https://example.invalid/notes" }] };

    expect(WorkflowPresentationSchema.safeParse(stored).success).toBe(false);
  });

  it("keeps the same action when the host has installed its function", () => {
    render(
      <ArtifactActions
        runId="run-unnamed"
        actions={[ACTION]}
        artifacts={[REF]}
        handlers={{ saveNote: () => undefined }}
      />,
    );

    expect(screen.getByRole("button", { name: "Keep it as a note" })).toBeEnabled();
  });
});
