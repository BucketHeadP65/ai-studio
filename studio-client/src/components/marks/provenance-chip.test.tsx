import { describe, expect, it } from "vitest";

import { render } from "@/shared/test-support/render";

import { ProvenanceChip, provenance } from "./provenance-chip";

// The sentences are the contract every screen shares, so every actor form
// and every gate count gets an exact-string test, not a substring match.

describe("provenance sentence builders", () => {
  it("names the person who approved", () => {
    expect(provenance.approvedByPerson("dana")).toBe("Approved by a person, dana.");
    expect(provenance.approvedByPerson()).toBe("Approved by a person.");
    expect(provenance.startedByPerson()).toBe("Started by a person.");
    expect(provenance.needsAPerson()).toBe("This gate needs a person, the agent cannot decide it.");
  });

  it("says a person rejected, with no count of its own", () => {
    expect(provenance.rejectedByPerson()).toBe("Rejected by a person.");
  });

  it("names the schedule that started the run", () => {
    expect(provenance.startedBySchedule("ingest daily")).toBe(
      "Started by a schedule, ingest daily.",
    );
  });

  it("names the person who started the run", () => {
    expect(provenance.startedByPerson("dana")).toBe("Started by a person, dana.");
  });

  it("names a run started from chat", () => {
    expect(provenance.startedFromChat()).toBe("Started from chat.");
  });

  it("phrases one agent decided gate in the singular", () => {
    expect(provenance.decidedByAgent(["quality-gate"])).toBe(
      "quality-gate passed. The orchestrator agent decided this, no person approved it.",
    );
  });

  it("phrases two agent decided gates joined with and", () => {
    expect(provenance.decidedByAgent(["a", "b"])).toBe(
      "a and b passed. The orchestrator agent decided these, no person approved them.",
    );
  });

  it("phrases three agent decided gates with an oxford comma", () => {
    expect(provenance.decidedByAgent(["a", "b", "c"])).toBe(
      "a, b, and c passed. The orchestrator agent decided these, no person approved them.",
    );
  });

  it("phrases one automatic gate in the singular", () => {
    expect(provenance.passedAutomatically(["size-gate"])).toBe(
      "size-gate was configured to pass without review, no person approved it.",
    );
  });

  it("phrases two automatic gates joined with and", () => {
    expect(provenance.passedAutomatically(["size-gate", "drift-gate"])).toBe(
      "size-gate and drift-gate were configured to pass without review, no person approved them.",
    );
  });

  it("phrases three automatic gates with an oxford comma", () => {
    expect(provenance.passedAutomatically(["a", "b", "c"])).toBe(
      "a, b, and c were configured to pass without review, no person approved them.",
    );
  });
});

describe("ProvenanceChip", () => {
  it("renders the heading and the sentence in the box variant", () => {
    const { getByText } = render(
      <ProvenanceChip actor="agent-decided" variant="box" heading="Decided by the agent">
        {provenance.decidedByAgent(["quality-gate"])}
      </ProvenanceChip>,
    );

    expect(getByText("Decided by the agent")).toBeInTheDocument();
    expect(getByText(provenance.decidedByAgent(["quality-gate"]))).toBeInTheDocument();
  });

  it("renders no heading in the inline variant, even when one is passed", () => {
    const { queryByText, getByText } = render(
      <ProvenanceChip actor="person-approved" heading="Decided by the agent">
        {provenance.approvedByPerson("dana")}
      </ProvenanceChip>,
    );

    expect(queryByText("Decided by the agent")).not.toBeInTheDocument();
    expect(getByText(provenance.approvedByPerson("dana"))).toBeInTheDocument();
  });

  it("defaults to the inline variant", () => {
    const { getByText } = render(
      <ProvenanceChip actor="schedule-started">
        {provenance.startedBySchedule("ingest daily")}
      </ProvenanceChip>,
    );

    expect(getByText(provenance.startedBySchedule("ingest daily"))).toBeInTheDocument();
  });

  it.each([
    "person-approved",
    "person-pending",
    "person-rejected",
    "agent-decided",
    "automatic",
    "schedule-started",
    "person-started",
  ] as const)("renders an icon for the %s actor", (actor) => {
    const { container } = render(<ProvenanceChip actor={actor}>Sentence</ProvenanceChip>);

    expect(container.querySelector("svg")).not.toBeNull();
  });
});
