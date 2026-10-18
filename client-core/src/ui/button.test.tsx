import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Button } from "./button";

describe("<Button />", () => {
  it("renders its children as the visible label", () => {
    render(<Button>Save changes</Button>);

    expect(screen.getByRole("button", { name: /save changes/i })).toBeInTheDocument();
  });

  it("defaults to type='button' so it does not auto-submit ancestor forms", () => {
    render(<Button>X</Button>);

    expect(screen.getByRole("button")).toHaveAttribute("type", "button");
  });

  it("calls onClick when clicked", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(<Button onClick={onClick}>Click me</Button>);

    await user.click(screen.getByRole("button"));

    expect(onClick).toHaveBeenCalledOnce();
  });

  it("blocks onClick when disabled", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();
    render(
      <Button onClick={onClick} disabled>
        Disabled
      </Button>,
    );

    await user.click(screen.getByRole("button"));

    expect(onClick).not.toHaveBeenCalled();
  });

  it("variant='danger' tints the surface red", () => {
    render(<Button variant="danger">Delete</Button>);

    const button = screen.getByRole("button");
    expect(button.className).toMatch(/danger/);
  });
});
