import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { render } from "@/shared/test-support/render";

import { TextViewer } from "./text-viewer";

describe("TextViewer", () => {
  it("renders nothing when closed", () => {
    render(<TextViewer open={false} title="spec_yaml" text="name: x" onClose={vi.fn()} />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders the text through markdown by default", () => {
    render(<TextViewer open title="a skill body" text="a **bold** word" onClose={vi.fn()} />);

    expect(screen.getByText("bold")).toBeInTheDocument();
    expect(screen.getByText("bold").tagName).toBe("STRONG");
  });

  it("renders the text verbatim in plain mode instead", () => {
    render(
      <TextViewer open title="spec_yaml" text="a **bold** word" mode="plain" onClose={vi.fn()} />,
    );

    expect(screen.getByText("a **bold** word")).toBeInTheDocument();
  });

  it("closes on the close button", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<TextViewer open title="spec_yaml" text="name: x" onClose={onClose} />);

    await user.click(screen.getByRole("button", { name: "Close drawer" }));

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("renders a worker's own messages as alternating owner/worker bubbles in conversation mode", () => {
    render(
      <TextViewer
        open
        title="Step draft"
        mode="conversation"
        messages={[
          { kind: "request", parts: [{ part_kind: "user-prompt", content: "write the draft" }] },
          { kind: "response", parts: [{ part_kind: "text", content: "here is the draft" }] },
        ]}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("write the draft")).toBeInTheDocument();
    expect(screen.getByText("here is the draft")).toBeInTheDocument();
    expect(screen.getByText("owner")).toBeInTheDocument();
    expect(screen.getByText("worker")).toBeInTheDocument();
  });

  it("skips a message that renders no text, and tolerates an unparseable one", () => {
    render(
      <TextViewer
        open
        title="Step draft"
        mode="conversation"
        messages={[
          {
            kind: "request",
            parts: [{ part_kind: "tool-return", tool_name: "search", content: { hits: 3 } }],
          },
          "not a message at all",
          { kind: "response", parts: [{ part_kind: "text", content: "done" }] },
        ]}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.getAllByText(/owner|worker/)).toHaveLength(1);
  });
});
