import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import type { RunView } from "@/api/sessions-types";
import { render } from "@/shared/test-support/render";

import { RunTurn } from "./run-turn";

function runFixture(overrides: Partial<RunView> = {}): RunView {
  return {
    session_id: "s1",
    workflow_id: "wf.lit-check.v5",
    run_title: "",
    state: "running",
    active: [],
    done: [],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: "2026-08-24T00:00:00.000Z",
    ...overrides,
  };
}

describe("RunTurn", () => {
  it("draws the agent bubble's own grid, byline, and run summary", () => {
    render(
      <RunTurn title="Literature check" run={runFixture()} graph={undefined} now={Date.now()} />,
    );

    expect(screen.getByText("Run")).toBeInTheDocument();
    expect(screen.getByText("Literature check")).toBeInTheDocument();
    expect(screen.getByText("wf.lit-check.v5")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("carries no turn-detail affordance of its own", () => {
    render(
      <RunTurn title="Literature check" run={runFixture()} graph={undefined} now={Date.now()} />,
    );

    expect(screen.queryByRole("button", { name: "View turn detail" })).not.toBeInTheDocument();
  });
});
