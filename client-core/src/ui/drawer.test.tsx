import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { backdropOf } from "../test-support/dialog";
import { Drawer } from "./drawer";

function HostWithOpener() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open
      </button>
      <Drawer open={open} title="Ask the agent" onClose={() => setOpen(false)}>
        <p>inside</p>
      </Drawer>
    </>
  );
}

describe("<Drawer />", () => {
  it("renders nothing while closed", () => {
    render(
      <Drawer open={false} title="Ask the agent" onClose={vi.fn()}>
        <p>inside</p>
      </Drawer>,
    );

    expect(screen.queryByText("inside")).not.toBeInTheDocument();
  });

  it("shows its title and children while open", () => {
    render(
      <Drawer open title="Ask the agent" onClose={vi.fn()}>
        <p>inside</p>
      </Drawer>,
    );

    expect(screen.getByRole("dialog", { name: "Ask the agent" })).toBeInTheDocument();
    expect(screen.getByText("inside")).toBeInTheDocument();
  });

  it("closes on Escape and on a pointer outside the panel", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Drawer open title="Ask the agent" onClose={onClose}>
        <p>inside</p>
      </Drawer>,
    );

    await user.keyboard("{Escape}");
    // The dialog arms its outside-pointer listener a tick after opening.
    await new Promise((resolve) => setTimeout(resolve, 0));
    const backdrop = backdropOf(screen.getByRole("dialog"));
    fireEvent.pointerDown(backdrop);
    fireEvent.click(backdrop);

    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("traps Tab inside the drawer while open", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button type="button">outside</button>
        <Drawer open title="Ask the agent" onClose={vi.fn()}>
          <button type="button">inside action</button>
        </Drawer>
      </>,
    );
    const outside = screen.getByText("outside");

    await user.tab();
    await user.tab();
    await user.tab();
    await user.tab();

    expect(outside).not.toHaveFocus();
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  });

  it("leaves the page behind clickable when non-modal, closing on the way", async () => {
    const onClose = vi.fn();
    const onOutside = vi.fn();
    const user = userEvent.setup();
    render(
      <>
        <button type="button" onClick={onOutside}>
          outside
        </button>
        <Drawer open modal={false} title="Ask the agent" onClose={onClose}>
          <p>inside</p>
        </Drawer>
      </>,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));

    await user.click(screen.getByText("outside"));

    expect(onOutside).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalled();
  });

  it("returns focus to the opener once closed", async () => {
    const user = userEvent.setup();
    render(<HostWithOpener />);

    const opener = screen.getByRole("button", { name: "open" });
    opener.focus();
    await user.click(opener);
    await user.click(screen.getByRole("button", { name: "Close drawer" }));

    expect(opener).toHaveFocus();
  });

  it("closes from its own close button", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Drawer open title="Ask the agent" onClose={onClose}>
        <p>inside</p>
      </Drawer>,
    );

    await user.click(screen.getByRole("button", { name: "Close drawer" }));

    expect(onClose).toHaveBeenCalledOnce();
  });
});
