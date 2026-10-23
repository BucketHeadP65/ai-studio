import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { StepOutline } from "@/api/inspection";
import { render } from "@/shared/test-support/render";

import { RunTimeline } from "./run-timeline";

const STEPS: StepOutline[] = [
  { step: 0, messages: 2, summary: "opened the run" },
  { step: 1, messages: 4, summary: "read the spec" },
  { step: 2, messages: 6, summary: "ran the workflow" },
];

describe("RunTimeline", () => {
  it("names each bar by its step's messages, marks the finding step and rings the selection", () => {
    render(
      <RunTimeline steps={STEPS} marked={new Map([[1, "finding"]])} selected={2} onSelect={() => {}} />,
    );

    const bars = screen.getAllByRole("button");
    expect(bars).toHaveLength(3);
    expect(bars[0]).toHaveAccessibleName("step 0, 2 messages");
    expect(bars[1]).toHaveAccessibleName("step 1, 4 messages");
    expect(bars[2]).toHaveAccessibleName("step 2, 6 messages");
    expect(bars[1]).toHaveClass("bg-accent");
    expect(bars[2]).toHaveClass("outline-2");
    expect(bars[0]).not.toHaveClass("outline-2");
  });

  it("calls onSelect with the step number of the bar clicked", async () => {
    const onSelect = vi.fn();
    render(<RunTimeline steps={STEPS} marked={new Map()} selected={null} onSelect={onSelect} />);

    await userEvent.click(screen.getAllByRole("button")[1]);

    expect(onSelect).toHaveBeenCalledWith(1);
  });

  it("renders the caption given", () => {
    render(
      <RunTimeline steps={STEPS} marked={new Map()} selected={null} onSelect={() => {}} caption={<p>step 1, 4 messages</p>} />,
    );

    expect(screen.getByText("step 1, 4 messages")).toBeInTheDocument();
  });

  it("reads one message as singular, in both the accessible name and the title", () => {
    const oneMessage: StepOutline[] = [{ step: 0, messages: 1, summary: "a single message" }];
    render(<RunTimeline steps={oneMessage} marked={new Map()} selected={null} onSelect={() => {}} />);

    const bar = screen.getByRole("button");
    expect(bar).toHaveAccessibleName("step 0, 1 message");
    expect(bar).toHaveAttribute("title", "step 0, 1 message, a single message");
  });
});
