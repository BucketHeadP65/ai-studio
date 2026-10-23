import { screen } from "@testing-library/dom";
import { describe, expect, it } from "vitest";

import type { RunView } from "@/api/sessions-types";
import type { WorkflowGraph } from "@/api/studio-types";
import { render } from "@/shared/test-support/render";

import { RunSummary } from "./run-summary";

function runFixture(overrides: Partial<RunView> = {}): RunView {
  return {
    session_id: "s1",
    workflow_id: "wf.lit-check.v5",
    run_title: "",
    state: "running",
    active: ["draft"],
    done: ["intake"],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: "2026-08-24T00:00:00.000Z",
    ...overrides,
  };
}

function graphFixture(): WorkflowGraph {
  return {
    name: "lit-check",
    version: 5,
    nodes: [
      {
        key: "intake",
        id: "intake",
        kind: "run",
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
      },
      {
        key: "draft",
        id: "draft",
        kind: "run",
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
      },
    ],
    edges: [{ from: "intake", to: "draft", kind: "wire", label: "" }],
  };
}

const NOW = new Date("2026-08-24T00:01:30.000Z").getTime();

describe("RunSummary", () => {
  it("names the run, its workflow, and its state", () => {
    render(
      <RunSummary title="Literature check" run={runFixture()} graph={graphFixture()} now={NOW} />,
    );

    expect(screen.getByText("Literature check")).toBeInTheDocument();
    expect(screen.getByText("wf.lit-check.v5")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("shows the elapsed time off the given clock", () => {
    render(
      <RunSummary title="Literature check" run={runFixture()} graph={graphFixture()} now={NOW} />,
    );

    expect(screen.getByText("1m 30s")).toBeInTheDocument();
  });

  it("draws the mini graph with the fleet's own fill rules", () => {
    render(
      <RunSummary title="Literature check" run={runFixture()} graph={graphFixture()} now={NOW} />,
    );

    expect(screen.getByText("intake")).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
  });

  it("links to the run's own page", () => {
    render(
      <RunSummary title="Literature check" run={runFixture()} graph={graphFixture()} now={NOW} />,
    );

    expect(screen.getByRole("link", { name: "Open the run" })).toHaveAttribute(
      "href",
      "/workflows/wf.lit-check.v5",
    );
  });

  it("carries no pause, resume, or stop control of its own", () => {
    render(
      <RunSummary title="Literature check" run={runFixture()} graph={graphFixture()} now={NOW} />,
    );

    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });
});
