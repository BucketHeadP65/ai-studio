import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { RunEnvelope } from "@/api/studio-types";
import { render } from "@/shared/test-support/render";

import { HistoryRail } from "./history-rail";

// The rail draws exactly the list it is handed, newest first as the caller
// already ordered it -- it never sorts or synthesizes a row of its own. The
// live row (outcome "live") carries no persisted record, so it gets neither a
// pin nor a delete.

function envelope(overrides: Partial<RunEnvelope> = {}): RunEnvelope {
  return {
    spend: null,
    result: { status: "unknown", summary: "", attempted: 0, succeeded: 0, skipped: 0, failed: 0, retry_attempts: 0, primary_input: null, generation: null, checks: [], artifacts: [] },
    run_id: "run-1",
    workflow_id: "wf.lit-check.v5",
    run_title: "",
    outcome: "completed",
    failure: "",
    started_at: "2026-08-20T10:00:00Z",
    ended_at: "2026-08-20T10:05:00Z",
    runner: "chat",
    session_id: "sess-old",
    params: {},
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    ...overrides,
  };
}

const LIVE = envelope({ run_id: "sess-1", outcome: "live", session_id: "sess-1", ended_at: "" });
const PAST = envelope({ run_id: "run-42" });

function baseProps() {
  return {
    onSelect: vi.fn(),
    onTogglePin: vi.fn(),
    pinningId: null,
    onDeleteRequest: vi.fn(),
  };
}

describe("HistoryRail", () => {
  it("renders nothing when there are no runs", () => {
    const { container } = render(<HistoryRail runs={[]} selectedRunId={null} {...baseProps()} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("shows Live pinned on top and selected by default (selectedRunId null)", () => {
    render(<HistoryRail runs={[LIVE, PAST]} selectedRunId={null} {...baseProps()} />);

    expect(screen.getByText("Live")).toBeInTheDocument();
    const liveRow = screen.getByText("Live").closest("button");
    expect(liveRow).toHaveAttribute("aria-pressed", "true");
  });

  it("carries an outcome word on every past run, and marks the selected one", () => {
    render(<HistoryRail runs={[LIVE, PAST]} selectedRunId="run-42" {...baseProps()} />);

    expect(screen.getByText("Completed")).toBeInTheDocument();
    const pastRow = screen.getByText("Completed").closest("button");
    expect(pastRow).toHaveAttribute("aria-pressed", "true");
    const liveRow = screen.getByText("Live").closest("button");
    expect(liveRow).toHaveAttribute("aria-pressed", "false");
  });

  it("names the picked run to the caller on click", async () => {
    const onSelect = vi.fn();
    render(
      <HistoryRail runs={[LIVE, PAST]} selectedRunId={null} {...baseProps()} onSelect={onSelect} />,
    );

    await userEvent.click(screen.getByText("Completed").closest("button")!);

    expect(onSelect).toHaveBeenCalledWith(PAST);
  });

  it("offers neither pin nor delete on the live row", () => {
    render(<HistoryRail runs={[LIVE]} selectedRunId={null} {...baseProps()} />);

    expect(screen.queryByRole("button", { name: /pin this run/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete this run" })).not.toBeInTheDocument();
  });

  it("toggles pin on a past run", async () => {
    const onTogglePin = vi.fn();
    render(
      <HistoryRail runs={[PAST]} selectedRunId={null} {...baseProps()} onTogglePin={onTogglePin} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Pin this run" }));

    expect(onTogglePin).toHaveBeenCalledWith(PAST);
  });

  it("shows Unpin once a run is pinned, filled", () => {
    render(
      <HistoryRail runs={[envelope({ pinned: true })]} selectedRunId={null} {...baseProps()} />,
    );

    const star = screen.getByRole("button", { name: "Unpin this run" });
    expect(star.querySelector("svg")).toHaveAttribute("fill", "currentColor");
  });

  it("requests delete on a past run", async () => {
    const onDeleteRequest = vi.fn();
    render(
      <HistoryRail
        runs={[PAST]}
        selectedRunId={null}
        {...baseProps()}
        onDeleteRequest={onDeleteRequest}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Delete this run" }));

    expect(onDeleteRequest).toHaveBeenCalledWith(PAST);
  });
});
