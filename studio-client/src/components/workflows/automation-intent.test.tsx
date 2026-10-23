import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Turn, WorkflowIntent } from "@/api/studio-types";
import { render } from "@/shared/test-support/render";

import { AutomationIntent, AutomationRequest, DesignExchange } from "./automation-intent";

// The two screens a person meets when they ask for an automation. The first asks for the task and
// nothing else. The second shows what was recorded, what is still open, and what was assumed on
// their behalf, and lets them change any of it.

const ASKED_FOR: WorkflowIntent = {
  request: "Every Monday, tell me which of my open questions the week's reading answered",
  expected_result: "A short list of questions with the reading that answers each one.",
  success_example: "",
  empty_input_behavior: "",
  failure_behavior: "",
  trigger: "scheduled",
  assumptions: ["The knowledge base to read is the one this session already names"],
};

describe("the first screen", () => {
  it("asks for the task and nothing else", () => {
    render(<AutomationRequest onSubmit={vi.fn()} busy={false} error={null} />);

    expect(
      screen.getByRole("heading", { name: "What should this automation do?" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(screen.queryByText(/What a finished run leaves you holding/)).not.toBeInTheDocument();
  });

  it("hands over the typed task, trimmed, and says nothing runs from it", async () => {
    const onSubmit = vi.fn();
    render(<AutomationRequest onSubmit={onSubmit} busy={false} error={null} />);

    await userEvent.type(screen.getByRole("textbox"), "  Sweep my open questions  ");
    await userEvent.click(screen.getByRole("button", { name: "Design it" }));

    expect(onSubmit).toHaveBeenCalledWith("Sweep my open questions");
    expect(screen.getByText(/Nothing runs and nothing is scheduled until you say so/)).toBeVisible();
  });

  it("will not send an empty task, and holds the box while a turn is in flight", () => {
    const { rerender } = render(
      <AutomationRequest onSubmit={vi.fn()} busy={false} error={null} />,
    );
    expect(screen.getByRole("button", { name: "Design it" })).toBeDisabled();

    rerender(<AutomationRequest onSubmit={vi.fn()} busy error={null} />);
    expect(screen.getByRole("button", { name: "Designing" })).toBeDisabled();
    expect(screen.getByRole("textbox")).toBeDisabled();
  });
});

describe("the intent panel", () => {
  it("says a draft records no request rather than drawing an empty panel", () => {
    render(
      <AutomationIntent
        intent={null}
        issues={[]}
        onRecord={vi.fn()}
        saving={false}
        error={null}
      />,
    );

    expect(
      screen.getByText(
        "Nothing records a request for this draft. Ask for a change below and the agent records what you asked for with it.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Save what you asked for" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Assumption 1")).not.toBeInTheDocument();
  });

  it("shows the request verbatim, every unanswered field, and each assumption as inferred", () => {
    render(
      <AutomationIntent
        intent={ASKED_FOR}
        issues={["success_example", "empty_input_behavior", "failure_behavior"]}
        onRecord={vi.fn()}
        saving={false}
        error={null}
      />,
    );

    expect(screen.getByText(ASKED_FOR.request)).toBeInTheDocument();
    expect(screen.getByText("3 answers are still open. It is fine to leave them, and the draft can still be saved.")).toBeVisible();
    expect(screen.getAllByText("Not stated yet")).toHaveLength(3);
    expect(screen.getByLabelText("Assumption 1")).toHaveValue(ASKED_FOR.assumptions[0]);
    expect(screen.getByText("Inferred")).toBeVisible();
    expect(screen.getByLabelText("Who starts a run")).toHaveValue("scheduled");
  });

  it("says so plainly when every answer is in", () => {
    render(
      <AutomationIntent
        intent={{ ...ASKED_FOR, success_example: "a", empty_input_behavior: "b", failure_behavior: "c", assumptions: [] }}
        issues={[]}
        onRecord={vi.fn()}
        saving={false}
        error={null}
      />,
    );

    expect(screen.queryByText(/answers are still open/)).not.toBeInTheDocument();
    expect(screen.getByText("Nothing was assumed. Every answer above is one you gave.")).toBeVisible();
  });

  it("records the whole statement once an assumption is edited, and not before", async () => {
    const onRecord = vi.fn();
    render(
      <AutomationIntent
        intent={ASKED_FOR}
        issues={["success_example", "empty_input_behavior", "failure_behavior"]}
        onRecord={onRecord}
        saving={false}
        error={null}
      />,
    );
    expect(screen.getByRole("button", { name: "Save what you asked for" })).toBeDisabled();

    await userEvent.clear(screen.getByLabelText("Assumption 1"));
    await userEvent.type(screen.getByLabelText("Assumption 1"), "I pick the knowledge base myself");
    await userEvent.click(screen.getByRole("button", { name: "Save what you asked for" }));

    expect(onRecord).toHaveBeenCalledTimes(1);
    expect(onRecord).toHaveBeenCalledWith({
      ...ASKED_FOR,
      assumptions: ["I pick the knowledge base myself"],
    });
  });

  it("drops an assumption the person turns down", async () => {
    const onRecord = vi.fn();
    render(
      <AutomationIntent
        intent={ASKED_FOR}
        issues={[]}
        onRecord={onRecord}
        saving={false}
        error={null}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Drop assumption 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Save what you asked for" }));

    expect(onRecord).toHaveBeenCalledWith({ ...ASKED_FOR, assumptions: [] });
  });

  it("says a save records the request and nothing else", () => {
    render(
      <AutomationIntent
        intent={ASKED_FOR}
        issues={[]}
        onRecord={vi.fn()}
        saving={false}
        error={null}
      />,
    );

    expect(
      screen.getByText("Saving this records the request. It does not save the workflow and it does not run it."),
    ).toBeVisible();
  });

  it("shows a refusal from the route rather than swallowing it", () => {
    render(
      <AutomationIntent
        intent={ASKED_FOR}
        issues={[]}
        onRecord={vi.fn()}
        saving={false}
        error="no session with that id here"
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("no session with that id here");
  });
});

describe("the exchange beside a drafted automation", () => {
  const TURNS: readonly Turn[] = [
    { who: "you", text: "Every Monday, sweep my open questions" },
    { who: "agent", text: "Here is the shape it would run. Read it and tell me what to change." },
  ];

  it("shows what was said, so the proposal can be answered where it is read", () => {
    render(
      <DesignExchange
        turns={TURNS}
        onSend={vi.fn()}
        busy={false}
        error={null}
        chatHref="/chat?session=s1"
      />,
    );

    expect(screen.getByText("Every Monday, sweep my open questions")).toBeInTheDocument();
    expect(
      screen.getByText("Here is the shape it would run. Read it and tell me what to change."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open this session in the chat" })).toHaveAttribute(
      "href",
      "/chat?session=s1",
    );
  });

  it("sends what should change, trimmed, and empties the box", async () => {
    const onSend = vi.fn();
    render(
      <DesignExchange
        turns={TURNS}
        onSend={onSend}
        busy={false}
        error={null}
        chatHref="/chat?session=s1"
      />,
    );

    await userEvent.type(screen.getByLabelText("What should change?"), "  Split the second step  ");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(onSend).toHaveBeenCalledWith("Split the second step");
    expect(screen.getByLabelText("What should change?")).toHaveValue("");
    expect(
      screen.getByText(/Nothing runs and nothing is scheduled until you say so/),
    ).toBeVisible();
  });

  it("will not send an empty message, and holds the box while a turn is in flight", () => {
    const { rerender } = render(
      <DesignExchange
        turns={TURNS}
        onSend={vi.fn()}
        busy={false}
        error={null}
        chatHref="/chat?session=s1"
      />,
    );
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    rerender(
      <DesignExchange turns={TURNS} onSend={vi.fn()} busy error={null} chatHref="/chat?session=s1" />,
    );
    expect(screen.getByRole("button", { name: "Designing" })).toBeDisabled();
    expect(screen.getByLabelText("What should change?")).toBeDisabled();
  });

  it("says plainly when nothing has been said yet", () => {
    render(
      <DesignExchange
        turns={[]}
        onSend={vi.fn()}
        busy={false}
        error={null}
        chatHref="/chat?session=s1"
      />,
    );

    expect(screen.getByText("Nothing yet.")).toBeVisible();
  });
});
