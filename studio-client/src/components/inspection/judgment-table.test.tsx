import { fireEvent, screen, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Blame } from "@/api/inspection";
import { render } from "@/shared/test-support/render";

import { JudgmentTable } from "./judgment-table";

const SPANS = ["5.8.0", "5.9.0"];

const BLAMES: Blame[] = [
  {
    root_cause: "agent_reasoning",
    root_cause_text: "the agent erred though it had what it needed",
    surface_kind: "model_tier",
    surface_name: "",
    why: "the return at 5.8.0 warned it plainly, and the thinking at 5.9.0 ran no check",
  },
  {
    root_cause: "tool_result_shape",
    root_cause_text: "the return mixed the warning with a confirmation",
    surface_kind: "tool",
    surface_name: "fetch_workflow_spec",
    why: "the fit field is one blob",
  },
];

function renderTable(overrides: Partial<Parameters<typeof JudgmentTable>[0]> = {}) {
  const props = {
    blames: BLAMES,
    spans: SPANS,
    onHover: vi.fn(),
    onPick: vi.fn(),
    onChallenge: vi.fn(),
    ...overrides,
  };
  render(<JudgmentTable {...props} />);
  return props;
}

// The two blame rows only, in order, telling them apart from the header row by its own cells'
// role (`columnheader`, not `cell`).
function blameRows(): HTMLElement[] {
  return screen.getAllByRole("row").filter((row) => within(row).queryAllByRole("cell").length > 0);
}

describe("JudgmentTable", () => {
  it("is a table naming itself, with a header row and one row per blame", () => {
    renderTable();

    expect(screen.getByRole("table", { name: "What is to blame" })).toBeInTheDocument();
    expect(screen.getByText("Root cause")).toBeInTheDocument();
    expect(screen.getByText("Fix lands on")).toBeInTheDocument();
    expect(screen.getByText("Why")).toBeInTheDocument();
    expect(screen.getByText("Cites")).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(blameRows()).toHaveLength(2);
  });

  it("shows the root cause and fix surface in words", () => {
    renderTable();

    expect(screen.getByText("Agent reasoning")).toBeInTheDocument();
    expect(screen.getByText("the agent erred though it had what it needed")).toBeInTheDocument();
    expect(screen.getByText("fetch_workflow_spec")).toBeInTheDocument();
  });

  it("shows nothing named to edit when the blame's surface has no name", () => {
    renderTable();

    expect(screen.getByText("Nothing named to edit")).toBeInTheDocument();
  });

  it("cites every span a why names, and every span when it names none", () => {
    renderTable();

    const rows = blameRows();
    const cites1 = within(rows[0].children[4] as HTMLElement);
    const cites2 = within(rows[1].children[4] as HTMLElement);
    expect(cites1.getAllByRole("button").map((button) => button.textContent)).toEqual(["5.8.0", "5.9.0"]);
    expect(cites2.getAllByRole("button").map((button) => button.textContent)).toEqual(["5.8.0", "5.9.0"]);
  });

  it("picks a span when a cite is clicked", async () => {
    const { onPick } = renderTable();

    const rows = blameRows();
    await userEvent.click(within(rows[0].children[4] as HTMLElement).getAllByRole("button")[0]);

    expect(onPick).toHaveBeenCalledWith("5.8.0");
  });

  it("picks a span when its coordinate in the why is clicked", async () => {
    const { onPick } = renderTable();

    const rows = blameRows();
    await userEvent.click(within(rows[0].children[3] as HTMLElement).getAllByRole("button")[0]);

    expect(onPick).toHaveBeenCalledWith("5.8.0");
  });

  it("hovers a row on enter and clears it on leave", () => {
    const { onHover } = renderTable();

    const rows = blameRows();
    fireEvent.mouseOver(rows[0]);
    fireEvent.mouseOut(rows[0]);

    expect(onHover).toHaveBeenNthCalledWith(1, 0);
    expect(onHover).toHaveBeenNthCalledWith(2, null);
  });

  it("challenges the row its button was clicked on", async () => {
    const { onChallenge } = renderTable();

    await userEvent.click(screen.getAllByRole("button", { name: "Challenge" })[1]);

    expect(onChallenge).toHaveBeenCalledWith(1);
  });
});
