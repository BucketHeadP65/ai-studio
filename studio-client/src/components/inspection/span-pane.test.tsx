import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { type ReactElement, useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { Evidence } from "@/api/inspection";
import { render } from "@/test-support/render";

import { SpanPane } from "./span-pane";

// A wrapper that actually owns `picked`, the way `FindingDetailView` does -- `SpanPane` itself
// takes it as a controlled prop, so testing what a real pick does needs a real picker behind it.
function Picker({ evidence }: { evidence: Evidence[] }): ReactElement {
  const [picked, setPicked] = useState(evidence[0]?.span ?? "");
  return <SpanPane evidence={evidence} phrases={[]} picked={picked} onPick={setPicked} onReadStep={() => {}} />;
}

const THIRTY_LINES = Array.from({ length: 30 }, (_, index) => `line ${index}`).join("\n");

const EVIDENCE: Evidence[] = [
  { span: "5.8.0", label: "step 5, the return of fetch_workflow_spec", kind: "tool-return", text: THIRTY_LINES },
  { span: "5.9.0", label: "step 5, the model's thinking", kind: "thinking", text: "The spec fits here, the leaves are registered." },
  { span: "5.9.2", label: "step 5, the model's call to research-digest", kind: "tool-call", text: "name: research-digest" },
];

describe("SpanPane", () => {
  it("shows the picked span's position and label", () => {
    render(<SpanPane evidence={EVIDENCE} phrases={[]} picked="5.8.0" onPick={() => {}} onReadStep={() => {}} />);

    expect(screen.getByText("1 of 3")).toBeInTheDocument();
    expect(screen.getByText("step 5, the return of fetch_workflow_spec")).toBeInTheDocument();
  });

  it("steps to the next span from the one picked", async () => {
    const onPick = vi.fn();
    render(<SpanPane evidence={EVIDENCE} phrases={[]} picked="5.8.0" onPick={onPick} onReadStep={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: "Next span" }));

    expect(onPick).toHaveBeenCalledWith("5.9.0");
  });

  it("wraps to the last span from the first on Previous span", async () => {
    const onPick = vi.fn();
    render(<SpanPane evidence={EVIDENCE} phrases={[]} picked="5.8.0" onPick={onPick} onReadStep={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: "Previous span" }));

    expect(onPick).toHaveBeenCalledWith("5.9.2");
  });

  it("folds a long span past 20 lines", () => {
    render(<SpanPane evidence={EVIDENCE} phrases={[]} picked="5.8.0" onPick={() => {}} onReadStep={() => {}} />);

    expect(screen.getByText("Show all (30 lines)")).toBeInTheDocument();
  });

  it("reads the picked span's step in the run", async () => {
    const onReadStep = vi.fn();
    render(<SpanPane evidence={EVIDENCE} phrases={[]} picked="5.8.0" onPick={() => {}} onReadStep={onReadStep} />);

    await userEvent.click(screen.getByRole("button", { name: "Read step 5 in the run" }));

    expect(onReadStep).toHaveBeenCalledWith(5);
  });

  it("shows the span is not in the run and no footer for an unreadable piece", () => {
    const unreadable: Evidence[] = [{ span: "5.9.9", label: "step 5, a gone span", kind: "unreadable", text: "this span is not in the run" }];
    render(<SpanPane evidence={unreadable} phrases={[]} picked="5.9.9" onPick={() => {}} onReadStep={() => {}} />);

    expect(screen.getByText("This span is not in the run.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Read step/ })).not.toBeInTheDocument();
  });

  it("shows there is no evidence rather than crashing when evidence is empty", () => {
    render(<SpanPane evidence={[]} phrases={[]} picked="" onPick={() => {}} onReadStep={() => {}} />);

    expect(screen.getByText("There is no evidence to show.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("resets the fold when the picked span changes, even back to a span already unfolded", async () => {
    render(<Picker evidence={EVIDENCE} />);

    await userEvent.click(screen.getByText("Show all (30 lines)"));
    expect(screen.getByText("Show the first 20 lines")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Next span" }));
    await userEvent.click(screen.getByRole("button", { name: "Previous span" }));

    expect(screen.getByText("Show all (30 lines)")).toBeInTheDocument();
  });
});
