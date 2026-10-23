import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { backdropOf } from "../test-support/dialog";
import { Modal, ModalDescription, ModalTitle } from "./modal";

function HostWithOpener() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open
      </button>
      <Modal open={open} onClose={() => setOpen(false)}>
        <ModalTitle>Open folder</ModalTitle>
        <button type="button" onClick={() => setOpen(false)}>
          close
        </button>
      </Modal>
    </>
  );
}

describe("<Modal />", () => {
  it("renders nothing while closed", () => {
    render(
      <Modal open={false} onClose={vi.fn()}>
        <ModalTitle>Open folder</ModalTitle>
      </Modal>,
    );

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("is labelled by its title while open", () => {
    render(
      <Modal open onClose={vi.fn()}>
        <ModalTitle>Open folder</ModalTitle>
        <p>inside</p>
      </Modal>,
    );

    expect(screen.getByRole("dialog", { name: "Open folder" })).toBeInTheDocument();
    expect(screen.getByText("inside")).toBeInTheDocument();
  });

  it("closes on Escape and on a pointer on the backdrop", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Modal open onClose={onClose}>
        <ModalTitle>Open folder</ModalTitle>
      </Modal>,
    );

    await user.keyboard("{Escape}");
    // The dialog arms its outside-pointer listener a tick after opening.
    await new Promise((resolve) => setTimeout(resolve, 0));
    const backdrop = backdropOf(screen.getByRole("dialog"));
    fireEvent.pointerDown(backdrop);
    fireEvent.click(backdrop);

    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("traps Tab inside the panel while open", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button type="button">outside</button>
        <Modal open onClose={vi.fn()}>
          <ModalTitle>Open folder</ModalTitle>
          <button type="button">inside action</button>
        </Modal>
      </>,
    );
    const outside = screen.getByText("outside");

    await user.tab();
    await user.tab();
    await user.tab();

    expect(outside).not.toHaveFocus();
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true);
  });

  it("returns focus to the opener once closed", async () => {
    const user = userEvent.setup();
    render(<HostWithOpener />);

    const opener = screen.getByRole("button", { name: "open" });
    opener.focus();
    await user.click(opener);
    await user.click(screen.getByRole("button", { name: "close" }));

    expect(opener).toHaveFocus();
  });

  it("wires aria-describedby to a rendered ModalDescription, with no console warning", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(
      <Modal open onClose={vi.fn()}>
        <ModalTitle>Remove project</ModalTitle>
        <ModalDescription>This cannot be undone.</ModalDescription>
      </Modal>,
    );

    const describedById = screen.getByRole("dialog").getAttribute("aria-describedby");
    expect(describedById).not.toBeNull();
    const description = describedById === null ? null : document.getElementById(describedById);
    expect(description).toHaveTextContent("This cannot be undone.");
    expect(warnSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();

    warnSpy.mockRestore();
    errorSpy.mockRestore();
  });

  it("logs no console warning when a Modal has no description", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);

    render(
      <Modal open onClose={vi.fn()}>
        <ModalTitle>Open folder</ModalTitle>
      </Modal>,
    );

    expect(warnSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();

    warnSpy.mockRestore();
    errorSpy.mockRestore();
  });

  it("merges panel classes over the defaults", () => {
    render(
      <Modal open onClose={vi.fn()} className="max-w-2xl">
        <ModalTitle>Wide</ModalTitle>
      </Modal>,
    );

    const dialog = screen.getByRole("dialog");
    expect(dialog.className).toContain("max-w-2xl");
    expect(dialog.className).not.toContain("max-w-md");
  });
});
