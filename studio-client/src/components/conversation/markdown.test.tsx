import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AgentMarkdown } from "./markdown";

// jsdom computes no layout, so the guarantee these pin is the one the browser reads off the
// classes: the block never takes its width from its content, and a fence scrolls inside itself.

describe("AgentMarkdown", () => {
  it("takes its width from the container and breaks a long word", () => {
    const { container } = render(<AgentMarkdown text={`a${"b".repeat(4000)}c`} />);

    const block = container.firstElementChild;
    expect(block?.className).toContain("min-w-0");
    expect(block?.className).toContain("max-w-full");
    expect(block?.className).toContain("break-words");
  });

  it("keeps a wide fence scrolling inside itself", () => {
    const line = `{"message": "${"x".repeat(4000)}"}`;
    const { container } = render(<AgentMarkdown text={`\`\`\`json\n${line}\n\`\`\``} />);

    expect(container.querySelector("pre")).not.toBeNull();
    expect(container.firstElementChild?.className).toContain("[&_pre]:overflow-x-auto");
  });

  it("renders prose as prose", () => {
    render(<AgentMarkdown text="Found it, the file is on disk." />);

    expect(screen.getByText("Found it, the file is on disk.")).toBeDefined();
  });

  it("renders a link as a real anchor with its href", () => {
    const { container } = render(
      <AgentMarkdown text="see [the plan](https://example.test/plan)" />,
    );

    const anchor = container.querySelector("a");
    expect(anchor?.getAttribute("href")).toBe("https://example.test/plan");
    expect(container.querySelector("button")).toBeNull();
  });

  it("renders a fenced block through the highlighter's code body", () => {
    const { container } = render(<AgentMarkdown text={"```ts\nconst a = 1;\n```"} />);

    expect(container.querySelector('[data-streamdown="code-block-body"]')).not.toBeNull();
  });

  it("never shows half-streamed emphasis markers as literal text", () => {
    const { container } = render(<AgentMarkdown text="Reading **the pla" />);

    expect(container.textContent).toContain("Reading");
    expect(container.textContent).not.toContain("**");
  });
});
