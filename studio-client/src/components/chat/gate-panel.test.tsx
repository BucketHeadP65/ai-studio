import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PendingGate } from "@/api/studio-types";
import { render } from "@/shared/test-support/render";

import { GatePanel } from "./gate-panel";

// The one gate a person answers. Approve stays one click. Reject stays one
// click too, with no feedback. Reject with feedback reveals a textarea, and
// only that path carries text on the rejection it posts.

const PENDING: PendingGate = {
  gate_id: "gate-1",
  workflow: "lit-check",
  reviewing: '{"title": "a draft"}',
  rejection_reruns: "compose",
};

describe("GatePanel", () => {
  it("renders the state pill and the provenance sentence naming the person who must decide", () => {
    render(<GatePanel pending={PENDING} onDecide={vi.fn()} deciding={false} />);

    expect(screen.getByText("Waiting on you")).toBeInTheDocument();
    expect(
      screen.getByText("This gate needs a person, the agent cannot decide it."),
    ).toBeInTheDocument();
  });

  it("approves with one click and no feedback", async () => {
    const onDecide = vi.fn();
    render(<GatePanel pending={PENDING} onDecide={onDecide} deciding={false} />);

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(onDecide).toHaveBeenCalledWith(true);
  });

  it("rejects with one click and no feedback", async () => {
    const onDecide = vi.fn();
    render(<GatePanel pending={PENDING} onDecide={onDecide} deciding={false} />);

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));

    expect(onDecide).toHaveBeenCalledWith(false);
  });

  it("hides the feedback textarea until Reject with feedback is clicked", () => {
    render(<GatePanel pending={PENDING} onDecide={vi.fn()} deciding={false} />);

    expect(screen.queryByRole("textbox", { name: "Feedback" })).not.toBeInTheDocument();
  });

  it("reveals a textarea on Reject with feedback, and posts the rejection with the typed text", async () => {
    const onDecide = vi.fn();
    render(<GatePanel pending={PENDING} onDecide={onDecide} deciding={false} />);

    await userEvent.click(screen.getByRole("button", { name: "Reject with feedback" }));
    const box = screen.getByRole("textbox", { name: "Feedback" });
    await userEvent.type(box, "the risk section is thin");
    await userEvent.click(screen.getByRole("button", { name: "Send rejection" }));

    expect(onDecide).toHaveBeenCalledWith(false, "the risk section is thin");
  });

  it("closes the feedback textarea and clears it after sending", async () => {
    render(<GatePanel pending={PENDING} onDecide={vi.fn()} deciding={false} />);

    await userEvent.click(screen.getByRole("button", { name: "Reject with feedback" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Feedback" }), "needs more detail");
    await userEvent.click(screen.getByRole("button", { name: "Send rejection" }));

    expect(screen.queryByRole("textbox", { name: "Feedback" })).not.toBeInTheDocument();
  });

  it("disables Approve, Reject, and Reject with feedback while deciding", () => {
    render(<GatePanel pending={PENDING} onDecide={vi.fn()} deciding={true} />);

    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject with feedback" })).toBeDisabled();
  });

  describe("chrome bare", () => {
    it("drops its own header, showing the buttons and payload with no duplicate pill", () => {
      render(<GatePanel pending={PENDING} onDecide={vi.fn()} deciding={false} chrome="bare" />);

      expect(screen.queryByText("Waiting on you")).not.toBeInTheDocument();
      expect(screen.queryByText(/is waiting for you at/)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
      expect(screen.getByText(PENDING.reviewing)).toBeInTheDocument();
    });

    it("keeps the rejection-reruns fact as a caption line under the buttons", () => {
      render(<GatePanel pending={PENDING} onDecide={vi.fn()} deciding={false} chrome="bare" />);

      expect(
        screen.getByText(
          (_, element) =>
            element?.tagName.toLowerCase() === "p" &&
            element.textContent === "(a rejection reruns from compose)",
        ),
      ).toBeInTheDocument();
    });

    it("still posts a decision the same way", async () => {
      const onDecide = vi.fn();
      render(<GatePanel pending={PENDING} onDecide={onDecide} deciding={false} chrome="bare" />);

      await userEvent.click(screen.getByRole("button", { name: "Approve" }));

      expect(onDecide).toHaveBeenCalledWith(true);
    });
  });
  it("boxes the reviewed text only as a card, a thread lets it flow so the thread stays the one scroller", () => {
    const { unmount } = render(<GatePanel pending={PENDING} onDecide={vi.fn()} deciding={false} />);
    expect(screen.getByText(PENDING.reviewing)).toHaveClass("max-h-48");
    unmount();
    render(<GatePanel pending={PENDING} onDecide={vi.fn()} deciding={false} chrome="bare" />);
    expect(screen.getByText(PENDING.reviewing)).not.toHaveClass("max-h-48");
  });
});
