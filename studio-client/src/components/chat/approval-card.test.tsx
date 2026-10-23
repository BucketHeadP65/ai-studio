import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { render } from "@/shared/test-support/render";

import { ApprovalCard } from "./approval-card";

const ACT_A = {
  approvalId: "c-2",
  toolCallId: "c-2",
  toolName: "toggle_plugin",
  input: { name: "sql", open: true },
};
const ACT_B = {
  approvalId: "c-3",
  toolCallId: "c-3",
  toolName: "run_control",
  input: { session_id: "s-1", act: "pause" },
};

describe("ApprovalCard", () => {
  it("names each act, approves, denies with a reason, and hands the composer back", async () => {
    const onRespond = vi.fn();
    const onReplyInstead = vi.fn();
    render(
      <ApprovalCard
        pending={[ACT_A, ACT_B]}
        onRespond={onRespond}
        onReplyInstead={onReplyInstead}
        busy={false}
      />,
    );
    expect(screen.getByText("toggle_plugin")).toBeInTheDocument();
    expect(screen.getByText(/"name": "sql"/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve toggle_plugin" }));
    expect(onRespond).toHaveBeenCalledWith("c-2", true, undefined);
    // The reason box answers whichever block's Deny sends it. It goes to the act still
    // waiting: the approved one is answered and cannot be answered twice.
    await userEvent.type(screen.getByRole("textbox", { name: "Reason" }), "not now");
    await userEvent.click(screen.getByRole("button", { name: "Deny run_control" }));
    expect(onRespond).toHaveBeenCalledWith("c-3", false, "not now");
    await userEvent.click(screen.getByRole("button", { name: "Reply instead" }));
    expect(onReplyInstead).toHaveBeenCalled();
  });

  it("sends undefined, not an empty string, when Deny fires with nothing typed in Reason", async () => {
    const onRespond = vi.fn();
    render(
      <ApprovalCard
        pending={[ACT_A]}
        onRespond={onRespond}
        onReplyInstead={vi.fn()}
        busy={false}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Deny toggle_plugin" }));
    expect(onRespond).toHaveBeenCalledWith("c-2", false, undefined);
  });

  it("locks an act's own block the instant it is answered, before any round trip, and leaves a different pending act untouched", async () => {
    const onRespond = vi.fn();
    render(
      <ApprovalCard
        pending={[ACT_A, ACT_B]}
        onRespond={onRespond}
        onReplyInstead={vi.fn()}
        busy={false}
      />,
    );
    const approveA = screen.getByRole("button", { name: "Approve toggle_plugin" });
    await userEvent.click(approveA);
    expect(onRespond).toHaveBeenCalledTimes(1);
    expect(onRespond).toHaveBeenCalledWith("c-2", true, undefined);

    // The block disables itself the instant the click fires, before any round trip -- a real
    // double click, or two fast clicks, must not fire this same block a second time.
    expect(approveA).toBeDisabled();

    // The other pending act was never part of that click and stays fully live and separate.
    expect(screen.getByRole("button", { name: "Approve run_control" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Deny run_control" })).toBeEnabled();
  });

  it("guards Reply instead the same way, so a second click never fires it twice", async () => {
    const onReplyInstead = vi.fn();
    render(
      <ApprovalCard
        pending={[ACT_A]}
        onRespond={vi.fn()}
        onReplyInstead={onReplyInstead}
        busy={false}
      />,
    );
    const reply = screen.getByRole("button", { name: "Reply instead" });
    await userEvent.click(reply);
    expect(onReplyInstead).toHaveBeenCalledTimes(1);
    expect(reply).toBeDisabled();
  });
});
