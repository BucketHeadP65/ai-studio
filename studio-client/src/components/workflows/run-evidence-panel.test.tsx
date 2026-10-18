import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import type { ArtifactRef, OwnerReview, WorkflowDetail } from "@/api/studio-types";
import { provenance } from "@/components/marks/provenance-chip";
import { render } from "@/test-support/render";

import { RunEvidencePanel } from "./run-evidence-panel";

const EARLY_DRAFT: ArtifactRef = {
  id: "draft",
  label: "The draft",
  run_id: "run-1",
  node_id: "draft_report",
  turn: null,
  value_digest: "a".repeat(64),
  media_type: "text/markdown",
};

const LATER_DRAFT: ArtifactRef = { ...EARLY_DRAFT, value_digest: "b".repeat(64) };

function review(overrides: Partial<OwnerReview> = {}): OwnerReview {
  return {
    review_id: "r1",
    actor: "Alex",
    outcome: "accepted",
    reason: "It answered the question.",
    artifact_refs: [EARLY_DRAFT],
    source: "app:harness",
    at: "2026-09-16T09:10:00Z",
    ...overrides,
  };
}

function trust(overrides: Partial<WorkflowDetail["trust"]> = {}): WorkflowDetail["trust"] {
  return {
    level: "machine-confirmed",
    runs_with_evidence: 3,
    failed_runs: 1,
    approved_by: [],
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
    ...overrides,
  };
}

describe("RunEvidencePanel", () => {
  it("states the level and the run counts behind it", () => {
    render(<RunEvidencePanel trust={trust()} />);

    expect(screen.getByText("confirmed by runs")).toBeInTheDocument();
    expect(screen.getByText("3 completed runs, 1 failed runs.")).toBeInTheDocument();
  });

  it("adds the structural mean only once something has graded it", () => {
    const { rerender } = render(<RunEvidencePanel trust={trust()} />);
    expect(screen.queryByText(/Structural mean/)).not.toBeInTheDocument();

    rerender(
      <RunEvidencePanel trust={trust({ structural_grades: 4, structural_mean: 0.825 })} />,
    );
    expect(screen.getByText(/Structural mean 0.82 over 4 grades\./)).toBeInTheDocument();
  });

  it("shows no gate decisions block when the run named none", () => {
    render(<RunEvidencePanel trust={trust()} />);
    expect(screen.queryByText("Who decided each gate")).not.toBeInTheDocument();
  });

  it("labels who decided each gate, orchestrated ids apart from approved and auto ones", () => {
    render(
      <RunEvidencePanel
        trust={trust()}
        gateDecisions={{ approved: ["review"], orchestrated: ["ship"], auto: ["notify"] }}
      />,
    );

    expect(screen.getByText("review")).toBeInTheDocument();
    expect(screen.getByText(provenance.approvedByPerson())).toBeInTheDocument();
    expect(screen.getByText("Approved by a person.")).toBeInTheDocument();
    expect(screen.getByText("ship")).toBeInTheDocument();
    expect(screen.getByText(provenance.decidedByAgent(["ship"]))).toBeInTheDocument();
    expect(screen.getByText("notify")).toBeInTheDocument();
    expect(screen.getByText(provenance.passedAutomatically(["notify"]))).toBeInTheDocument();
  });
});

describe("RunEvidencePanel owner review", () => {
  it("shows no owner review block at all when no run is in view", () => {
    render(<RunEvidencePanel trust={trust()} />);

    expect(document.querySelector('[data-section="owner-review"]')).toBeNull();
  });

  it("reads a reviewed-by-nobody run as unknown, never as accepted or useful", () => {
    render(
      <RunEvidencePanel
        trust={trust()}
        gateDecisions={{ approved: ["review"], orchestrated: [], auto: [] }}
        ownerReviews={[]}
      />,
    );

    const block = document.querySelector('[data-section="owner-review"]');
    expect(block).not.toBeNull();
    expect(block).toHaveTextContent("Not reviewed");
    expect(block).toHaveTextContent("whether it was any use is unknown");
    expect(block).not.toHaveTextContent(/Accepted/);
    expect(block).not.toHaveTextContent(/useful/i);
  });

  it("keeps the owner's reading apart from the run's own execution evidence", () => {
    render(
      <RunEvidencePanel
        trust={trust()}
        gateDecisions={{ approved: ["review"], orchestrated: [], auto: [] }}
        ownerReviews={[review()]}
      />,
    );

    const execution = document.querySelector('[data-section="execution-evidence"]');
    const owner = document.querySelector('[data-section="owner-review"]');
    expect(execution).toHaveTextContent("Who decided each gate");
    expect(execution).not.toHaveTextContent("Alex");
    expect(owner).toHaveTextContent("Alex");
    expect(owner).not.toHaveTextContent("Who decided each gate");
  });

  it("shows every review in the order it was recorded, the later one never replacing the earlier", () => {
    render(
      <RunEvidencePanel
        trust={trust()}
        ownerReviews={[
          review(),
          review({
            review_id: "r2",
            outcome: "rejected",
            reason: "The second section came back empty.",
            artifact_refs: [LATER_DRAFT],
            at: "2026-09-16T10:30:00Z",
          }),
        ]}
      />,
    );

    const rows = screen.getAllByRole("listitem").filter((row) => row.querySelector("time"));
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Accepted");
    expect(rows[0]).toHaveTextContent("It answered the question.");
    expect(rows[0]).toHaveTextContent("aaaaaaaaaaaa");
    expect(rows[1]).toHaveTextContent("Rejected");
    expect(rows[1]).toHaveTextContent("The second section came back empty.");
    expect(rows[1]).toHaveTextContent("bbbbbbbbbbbb");
  });

  it("names who decided and what they read on every review", () => {
    render(
      <RunEvidencePanel
        trust={trust()}
        ownerReviews={[review({ actor: "Someone else entirely" })]}
      />,
    );

    expect(screen.getByText("Someone else entirely")).toBeInTheDocument();
    expect(screen.getByText("The draft")).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaaaaaa")).toBeInTheDocument();
  });
});
