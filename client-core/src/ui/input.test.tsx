import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Input } from "./input";
import { Select } from "./select";
import { Textarea } from "./textarea";

describe("form primitives", () => {
  it("Input accepts typing", async () => {
    const user = userEvent.setup();
    render(<Input aria-label="Name" />);

    await user.type(screen.getByLabelText("Name"), "notebook");

    expect(screen.getByLabelText("Name")).toHaveValue("notebook");
  });

  it("Textarea accepts typing", async () => {
    const user = userEvent.setup();
    render(<Textarea aria-label="Message" />);

    await user.type(screen.getByLabelText("Message"), "hello");

    expect(screen.getByLabelText("Message")).toHaveValue("hello");
  });

  it("Select changes value from its options", async () => {
    const user = userEvent.setup();
    render(
      <Select aria-label="Theme" defaultValue="paper">
        <option value="paper">Paper</option>
        <option value="factory">Factory</option>
      </Select>,
    );

    await user.selectOptions(screen.getByLabelText("Theme"), "factory");

    expect(screen.getByLabelText("Theme")).toHaveValue("factory");
  });
});
