import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AssistantTurn } from "./assistant-turn";

import type { UIMessage } from "ai";

function assistant(parts: UIMessage["parts"]): UIMessage {
  return { id: "a1", role: "assistant", parts };
}

describe("AssistantTurn", () => {
  it("shows a running work tool as a card in the bubble", () => {
    render(
      <AssistantTurn
        message={assistant([
          { type: "step-start" },
          {
            type: "tool-read_file",
            toolCallId: "c-1",
            state: "input-available",
            input: { path: "docs/plan.md" },
          },
        ])}
        onInspect={() => {}}
      />,
    );

    expect(screen.getByText(/read_file/)).toBeInTheDocument();
    expect(screen.getByText(/^Running/)).toBeInTheDocument();
  });

  it("keeps a finished turn's work cards inside the unfolded trace", async () => {
    const user = userEvent.setup();
    render(
      <AssistantTurn
        message={assistant([
          { type: "step-start" },
          {
            type: "tool-read_file",
            toolCallId: "c-1",
            state: "output-available",
            input: { path: "docs/plan.md" },
            output: "# the plan",
          },
          { type: "step-start" },
          { type: "text", text: "Read it." },
        ])}
        onInspect={() => {}}
      />,
    );

    expect(screen.getByText(/read_file/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /1 earlier step/ }));
    expect(screen.queryByText(/read_file/)).not.toBeInTheDocument();
  });

  it("starts a settled turn folded when folding is preferred", () => {
    render(
      <AssistantTurn
        foldSettled
        message={assistant([
          { type: "step-start" },
          {
            type: "tool-read_file",
            toolCallId: "c-1",
            state: "output-available",
            input: { path: "docs/plan.md" },
            output: "# the plan",
          },
          { type: "step-start" },
          { type: "text", text: "Read it." },
        ])}
        onInspect={() => {}}
      />,
    );

    expect(screen.queryByText(/read_file/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /1 earlier step/ })).toBeInTheDocument();
  });

  it("adopts a later foldSettled value on its own, with no toggle in between", () => {
    const message = assistant([
      { type: "step-start" },
      {
        type: "tool-read_file",
        toolCallId: "c-1",
        state: "output-available",
        input: { path: "docs/plan.md" },
        output: "# the plan",
      },
      { type: "step-start" },
      { type: "text", text: "Read it." },
    ]);
    const { rerender } = render(
      <AssistantTurn foldSettled={false} message={message} onInspect={() => {}} />,
    );

    expect(screen.getByText(/read_file/)).toBeInTheDocument();

    rerender(<AssistantTurn foldSettled message={message} onInspect={() => {}} />);

    expect(screen.queryByText(/read_file/)).not.toBeInTheDocument();
  });

  it("carries the thinking mark while live and drops it once settled", () => {
    const message = assistant([{ type: "step-start" }, { type: "text", text: "Reading." }]);
    const { rerender } = render(<AssistantTurn live message={message} onInspect={() => {}} />);

    expect(screen.getByRole("status", { name: "Thinking" })).toBeInTheDocument();

    rerender(<AssistantTurn message={message} onInspect={() => {}} />);

    expect(screen.queryByRole("status", { name: "Thinking" })).not.toBeInTheDocument();
  });

  it("keeps every step's cards in view while the turn is live", () => {
    render(
      <AssistantTurn
        live
        message={assistant([
          { type: "step-start" },
          {
            type: "tool-read_file",
            toolCallId: "c-1",
            state: "output-available",
            input: { path: "docs/plan.md" },
            output: "# the plan",
          },
          { type: "step-start" },
          {
            type: "tool-run_command",
            toolCallId: "c-2",
            state: "input-available",
            input: { command: "ls" },
          },
        ])}
        onInspect={() => {}}
      />,
    );

    expect(screen.getByText(/read_file/)).toBeInTheDocument();
    expect(screen.getByText(/run_command/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /earlier step/ })).not.toBeInTheDocument();
  });

  it("folds two consecutive same-kind work acts into one row until opened", async () => {
    const user = userEvent.setup();
    render(
      <AssistantTurn
        message={assistant([
          { type: "step-start" },
          {
            type: "tool-run_command",
            toolCallId: "c-1",
            state: "output-available",
            input: { command: "ls" },
            output: "a\nb",
          },
          {
            type: "tool-run_command",
            toolCallId: "c-2",
            state: "output-available",
            input: { command: "pwd" },
            output: "/repo",
          },
        ])}
        onInspect={() => {}}
      />,
    );

    expect(screen.getByRole("button", { name: "Ran 2 commands" })).toBeInTheDocument();
    expect(screen.queryByText(/run_command/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Ran 2 commands" }));

    expect(screen.getAllByText(/run_command/)).toHaveLength(2);
  });

  it("keeps a live turn's own work group open, never folded behind a count", () => {
    render(
      <AssistantTurn
        live
        message={assistant([
          { type: "step-start" },
          {
            type: "tool-run_command",
            toolCallId: "c-1",
            state: "output-available",
            input: { command: "ls" },
            output: "a\nb",
          },
          {
            type: "tool-run_command",
            toolCallId: "c-2",
            state: "input-available",
            input: { command: "pwd" },
          },
        ])}
        onInspect={() => {}}
      />,
    );

    const fold = screen.getByRole("button", { name: "Ran 2 commands" });
    expect(fold).toHaveAttribute("aria-expanded", "true");
    expect(fold.getAttribute("aria-controls")).toBeTruthy();
    expect(screen.getAllByText(/run_command/)).toHaveLength(2);
  });

  it("defaults to the orchestrator label and shows the detail button when onInspect is given", () => {
    render(<AssistantTurn message={assistant([{ type: "text", text: "Hi." }])} onInspect={() => {}} />);

    expect(screen.getByText("Orchestrator agent")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View turn detail" })).toBeInTheDocument();
  });

  it("speaks as the given speaker and hides the detail button when onInspect is omitted", () => {
    render(<AssistantTurn message={assistant([{ type: "text", text: "Hi." }])} speaker="Step draft" />);

    expect(screen.getByText("Step draft")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View turn detail" })).not.toBeInTheDocument();
  });

  it("keeps a kind change between two runs as three separate cards", () => {
    render(
      <AssistantTurn
        message={assistant([
          { type: "step-start" },
          {
            type: "tool-run_command",
            toolCallId: "c-1",
            state: "output-available",
            input: { command: "ls" },
            output: "a",
          },
          {
            type: "tool-read_file",
            toolCallId: "c-2",
            state: "output-available",
            input: { path: "docs/plan.md" },
            output: "# the plan",
          },
          {
            type: "tool-run_command",
            toolCallId: "c-3",
            state: "output-available",
            input: { command: "pwd" },
            output: "/repo",
          },
        ])}
        onInspect={() => {}}
      />,
    );

    expect(screen.getAllByLabelText("Completed")).toHaveLength(3);
    expect(screen.getAllByText(/run_command/)).toHaveLength(2);
    expect(screen.getByText(/read_file/)).toBeInTheDocument();
  });
});
