import { screen, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { GraphNode, PendingGate, RunEnvelope, StepEvent } from "@/api/studio-types";
import { render } from "@/test-support/render";

import { GateTrail } from "./gate-trail";

// One row per visit a gate node made, read off the run that is showing: a person's own yes or
// no, this run's own orchestrator deciding instead, nobody asked at all, the one still open, and
// a gate the run never reached named plainly. A gate rejected then decided reads as two rows,
// the rejection first, in the order they happened.

function gateNode(id: string, kind: GraphNode["kind"] = "gate"): GraphNode {
  return {
    key: id,
    id,
    kind,
    signature: "",
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
  };
}

const STARTED_AT = "2026-08-20T10:00:00Z";

const NODES: GraphNode[] = [
  gateNode("approved-gate"),
  gateNode("rejected-gate"),
  gateNode("review-gate"),
  gateNode("orchestrated-gate", "orchestrator-gate"),
  gateNode("auto-gate"),
  gateNode("open-gate"),
  gateNode("not-reached-gate"),
];

const ENVELOPE: RunEnvelope = {
  spend: null,
  result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
  run_id: "run-1",
  workflow_id: "wf.lit-check.v5",
  run_title: "",
  outcome: "live",
  failure: "",
  started_at: STARTED_AT,
  ended_at: "",
  runner: "app:chat",
  session_id: "sess-1",
  params: {},
  gates_approved: ["approved-gate", "review-gate"],
  gates_rejected: { "rejected-gate": 1, "review-gate": 1 },
  gates_auto: ["auto-gate"],
  gates_orchestrated: ["orchestrated-gate"],
  pinned: false,
};

const EVENTS: StepEvent[] = [
  { seq: 1, node: "approved-gate", at: 12.4, finished: true, failed: false },
  { seq: 2, node: "rejected-gate", at: 5, finished: true, failed: false },
  { seq: 3, node: "review-gate", at: 20, finished: true, failed: false },
  { seq: 4, node: "review-gate", at: 45.6, finished: true, failed: false },
  { seq: 5, node: "orchestrated-gate", at: 3.1, finished: true, failed: false },
  { seq: 6, node: "auto-gate", at: 1, finished: true, failed: false },
  { seq: 7, node: "open-gate", at: 50, finished: false, failed: false },
];

const PENDING: PendingGate = {
  gate_id: "open-gate",
  workflow: "wf.lit-check.v5",
  reviewing: '{"title": "a draft"}',
  rejection_reruns: "compose",
};

function noop() {
  // decide is not under test in the cases that never open a row's controls
}

describe("GateTrail", () => {
  it("renders nothing when the graph has no gates", () => {
    const { container } = render(
      <GateTrail
        nodes={[]}
        envelope={ENVELOPE}
        waitingAt={null}
        events={[]}
        pending={null}
        onDecide={noop}
        deciding={false}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps the card at its own content height, so its own rows never get squeezed to a sliver", () => {
    const twoNodes = [gateNode("approved-gate"), gateNode("rejected-gate")];
    const { container } = render(
      <GateTrail
        nodes={twoNodes}
        envelope={ENVELOPE}
        waitingAt={null}
        events={EVENTS}
        pending={null}
        onDecide={noop}
        deciding={false}
      />,
    );

    expect(screen.getByText("approved-gate")).toBeInTheDocument();
    expect(screen.getByText("rejected-gate")).toBeInTheDocument();

    const card = container.firstElementChild as HTMLElement;
    expect(card).toHaveClass("shrink-0");
    expect(card.className).not.toMatch(/\bh-\[|\bmax-h-|\bh-\d/);
  });

  it("orders every row by the time it happened, a rejected-then-approved gate reading as two rows", () => {
    const { container } = render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt="open-gate"
        events={EVENTS}
        pending={PENDING}
        onDecide={noop}
        deciding={false}
      />,
    );

    const rows = Array.from(container.querySelectorAll('[class*="grid-cols-"]')).map(
      (row) => row.textContent ?? "",
    );

    expect(rows).toHaveLength(8);
    expect(rows[0]).toContain("Passed automatically");
    expect(rows[0]).toContain("auto-gate");
    expect(rows[1]).toContain("Decided by the agent");
    expect(rows[1]).toContain("orchestrated-gate");
    expect(rows[2]).toContain("Rejected by a person");
    expect(rows[2]).toContain("rejected-gate");
    expect(rows[3]).toContain("Approved by a person");
    expect(rows[3]).toContain("approved-gate");
    expect(rows[4]).toContain("Rejected by a person");
    expect(rows[4]).toContain("review-gate");
    expect(rows[4]).toContain("20.0s");
    expect(rows[5]).toContain("Approved by a person");
    expect(rows[5]).toContain("review-gate");
    expect(rows[5]).toContain("45.6s");
    expect(rows[6]).toContain("Waiting on you");
    expect(rows[6]).toContain("open-gate");
    expect(rows[7]).toContain("Not reached yet");
    expect(rows[7]).toContain("not-reached-gate");
  });

  it("shows the hot pill only while a gate is open, a mono decision count otherwise", () => {
    const idle = render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt={null}
        events={EVENTS}
        pending={null}
        onDecide={noop}
        deciding={false}
      />,
    );
    expect(idle.queryByText(/waiting on you$/)).not.toBeInTheDocument();
    expect(idle.getByText("Decisions")).toBeInTheDocument();
    const count = idle.getByText("Decisions").nextElementSibling;
    expect(count).toHaveTextContent("6");
    expect(count).toHaveClass("font-mono");
    idle.unmount();

    render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt="open-gate"
        events={EVENTS}
        pending={PENDING}
        onDecide={noop}
        deciding={false}
      />,
    );
    const pill = screen.getByText((_, element) => element?.textContent === "1 waiting on you");
    expect(pill.querySelector(".font-mono")).toHaveTextContent("1");
    expect(screen.queryByText("Decisions")).not.toBeInTheDocument();
  });

  it("a rejection without feedback is its heading alone, no sentence repeating it", () => {
    render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt={null}
        events={EVENTS}
        pending={null}
        onDecide={noop}
        deciding={false}
      />,
    );

    const row = screen.getByText("rejected-gate").closest('[class*="grid-cols-"]') as HTMLElement;
    expect(within(row).getByText("Rejected by a person")).toBeInTheDocument();
    expect(within(row).queryByText("Rejected by a person.")).toBeNull();
  });

  it("reads an orchestrator-decided gate, denying a person approved it", () => {
    render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt={null}
        events={EVENTS}
        pending={null}
        onDecide={noop}
        deciding={false}
      />,
    );

    expect(
      screen.getByText(
        "orchestrated-gate passed. The orchestrator agent decided this, no person approved it.",
      ),
    ).toBeInTheDocument();
  });

  it("reads an automatic gate, denying a person approved it", () => {
    render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt={null}
        events={EVENTS}
        pending={null}
        onDecide={noop}
        deciding={false}
      />,
    );

    expect(
      screen.getByText("auto-gate was configured to pass without review, no person approved it."),
    ).toBeInTheDocument();
  });

  it("shows no sentence and no time for a gate the run never reached", () => {
    render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt="open-gate"
        events={EVENTS}
        pending={null}
        onDecide={noop}
        deciding={false}
      />,
    );

    const heading = screen.getByText("Not reached yet");
    expect(heading).toHaveClass("text-label-3");
    const row = heading.closest('[class*="grid-cols-"]') as HTMLElement;
    expect(row.querySelector("p")).toBeNull();
    const timeCell = row.querySelector(".text-right");
    expect(timeCell).toHaveTextContent("");
  });

  it("highlights the open row with the warn wash and the vermilion left rule", () => {
    render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt="open-gate"
        events={EVENTS}
        pending={PENDING}
        onDecide={noop}
        deciding={false}
      />,
    );

    const heading = screen.getByText("Waiting on you");
    const row = heading.closest('[class*="grid-cols-"]') as HTMLElement;
    expect(row.className).toContain("bg-warn-soft");
    expect(row.className).toContain("border-l-warn");
    expect(
      screen.getByText("The run stops here until you approve or reject it."),
    ).toBeInTheDocument();
  });

  it("carries the decide controls inline on the open row, wired to the page's own decide mutation", async () => {
    const onDecide = vi.fn();
    render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt="open-gate"
        events={EVENTS}
        pending={PENDING}
        onDecide={onDecide}
        deciding={false}
      />,
    );

    expect(screen.getByText(PENDING.reviewing)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).toHaveBeenCalledWith(true);

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecide).toHaveBeenCalledWith(false);
  });

  it("shows no decide controls when nothing pending matches the open gate", () => {
    render(
      <GateTrail
        nodes={NODES}
        envelope={ENVELOPE}
        waitingAt="open-gate"
        events={EVENTS}
        pending={null}
        onDecide={noop}
        deciding={false}
      />,
    );

    expect(screen.getByText("Waiting on you")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});
