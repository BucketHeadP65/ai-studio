import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { backdropOf } from "../test-support/dialog";
import { ConfirmDialog } from "./confirm-dialog";

function defaults() {
  return {
    title: "Delete conversation",
    message: "This is permanent.",
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
  };
}

describe("<ConfirmDialog />", () => {
  it("renders nothing when open is false", () => {
    render(<ConfirmDialog open={false} {...defaults()} />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders the title + message + buttons when open", () => {
    render(<ConfirmDialog open={true} {...defaults()} />);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Delete conversation")).toBeInTheDocument();
    expect(screen.getByText("This is permanent.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeInTheDocument();
  });

  it("fires onConfirm exactly once when Confirm is clicked", async () => {
    const d = defaults();
    const user = userEvent.setup();
    render(<ConfirmDialog open={true} {...d} />);

    await user.click(screen.getByRole("button", { name: /confirm/i }));

    expect(d.onConfirm).toHaveBeenCalledOnce();
    expect(d.onCancel).not.toHaveBeenCalled();
  });

  it("fires onCancel when Cancel is clicked", async () => {
    const d = defaults();
    const user = userEvent.setup();
    render(<ConfirmDialog open={true} {...d} />);

    await user.click(screen.getByRole("button", { name: /^cancel$/i }));

    expect(d.onCancel).toHaveBeenCalledOnce();
    expect(d.onConfirm).not.toHaveBeenCalled();
  });

  it("fires onCancel when Escape is pressed", async () => {
    const d = defaults();
    const user = userEvent.setup();
    render(<ConfirmDialog open={true} {...d} />);

    await user.keyboard("{Escape}");

    expect(d.onCancel).toHaveBeenCalledOnce();
  });

  it("fires onCancel on a pointer outside the panel", async () => {
    const d = defaults();
    render(<ConfirmDialog open={true} {...d} />);
    // The dialog arms its outside-pointer listener a tick after opening.
    await new Promise((resolve) => setTimeout(resolve, 0));

    const backdrop = backdropOf(screen.getByRole("dialog"));
    fireEvent.pointerDown(backdrop);
    fireEvent.click(backdrop);

    expect(d.onCancel).toHaveBeenCalledOnce();
  });

  it("focuses Cancel first so a stray Enter cancels rather than confirms", () => {
    render(<ConfirmDialog open={true} {...defaults()} />);

    expect(screen.getByRole("button", { name: /^cancel$/i })).toHaveFocus();
  });

  it("traps Tab inside the dialog while open", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button type="button">outside</button>
        <ConfirmDialog open={true} {...defaults()} />
      </>,
    );
    const outside = screen.getByText("outside");

    await user.tab();
    await user.tab();
    await user.tab();

    expect(outside).not.toHaveFocus();
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  });

  it("disables both buttons while confirming so a second click cannot fire", async () => {
    const d = defaults();
    const user = userEvent.setup();
    render(<ConfirmDialog open={true} {...d} confirming={true} />);

    await user.click(screen.getByRole("button", { name: /working/i }));
    await user.click(screen.getByRole("button", { name: /^cancel$/i }));

    expect(d.onConfirm).not.toHaveBeenCalled();
    expect(d.onCancel).not.toHaveBeenCalled();
  });

  it("renders the danger variant when destructive is true", () => {
    render(<ConfirmDialog open={true} {...defaults()} destructive={true} confirmLabel="Delete" />);

    const confirm = screen.getByRole("button", { name: /^delete$/i });
    expect(confirm.className).toMatch(/danger/);
  });

  it("uses custom labels when provided", () => {
    render(
      <ConfirmDialog
        open={true}
        {...defaults()}
        confirmLabel="Yes, delete it"
        cancelLabel="Keep it"
      />,
    );

    expect(screen.getByRole("button", { name: /yes, delete it/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /keep it/i })).toBeInTheDocument();
  });
});
