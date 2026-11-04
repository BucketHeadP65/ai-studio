import { render, screen, waitFor } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { describe, expect, it } from "vitest";

import { AgentMarkdown } from "./markdown";

const FENCE_THEN_OPEN_BRACKET = "Here:\n\n```ts\nconst answer = 42;\n```\n\nSee [the plan";

function isCodeColoured(): boolean {
  const body = document.querySelector('[data-streamdown="code-block-body"]');
  return body !== null && /--sdm-c: ?#/.test(body.innerHTML);
}

// Whether the code body is coloured at each commit, read in a layout effect, before the browser paints.
const colouredAtCommit: boolean[] = [];

function CommitProbe(): null {
  useLayoutEffect(() => {
    colouredAtCommit.push(isCodeColoured());
  });
  return null;
}

function BubbleWithProbe({ live }: { live: boolean }) {
  return (
    <>
      <AgentMarkdown live={live} text={FENCE_THEN_OPEN_BRACKET} />
      <CommitProbe />
    </>
  );
}

// jsdom computes no layout, so the guarantee these pin is the one the browser reads off the
// classes: the block never takes its width from its content, and a fence scrolls inside itself.

describe("AgentMarkdown", () => {
  it("takes its width from the container and breaks a long word", () => {
    const { container } = render(<AgentMarkdown live={false} text={`a${"b".repeat(4000)}c`} />);

    const block = container.firstElementChild;
    expect(block?.className).toContain("min-w-0");
    expect(block?.className).toContain("max-w-full");
    expect(block?.className).toContain("break-words");
  });

  it("keeps a wide fence scrolling inside itself", () => {
    const line = `{"message": "${"x".repeat(4000)}"}`;
    const { container } = render(<AgentMarkdown live={false} text={`\`\`\`json\n${line}\n\`\`\``} />);

    expect(container.querySelector("pre")).not.toBeNull();
    expect(container.firstElementChild?.className).toContain("[&_pre]:overflow-x-auto");
  });

  it("renders prose as prose", () => {
    render(<AgentMarkdown live={false} text="Found it, the file is on disk." />);

    expect(screen.getByText("Found it, the file is on disk.")).toBeDefined();
  });

  it("renders a link as a real anchor with its href", () => {
    const { container } = render(
      <AgentMarkdown live={false} text="see [the plan](https://example.test/plan)" />,
    );

    const anchor = container.querySelector("a");
    expect(anchor?.getAttribute("href")).toBe("https://example.test/plan");
    expect(container.querySelector("button")).toBeNull();
  });

  it("renders a fenced block through the highlighter's code body", () => {
    const { container } = render(<AgentMarkdown live={false} text={"```ts\nconst a = 1;\n```"} />);

    expect(container.querySelector('[data-streamdown="code-block-body"]')).not.toBeNull();
  });

  it("never shows half-streamed emphasis markers as literal text while the text streams", () => {
    const { container } = render(<AgentMarkdown live text="Reading **the pla" />);

    expect(container.textContent).toContain("Reading");
    expect(container.textContent).not.toContain("**");
  });

  it("closes an unmatched bracket while the text streams", () => {
    const { container } = render(<AgentMarkdown live text="see [the pla" />);

    expect(container.innerHTML).toContain("streamdown:incomplete-link");
  });

  it("shows finished text with an unmatched bracket as written, with no placeholder link", () => {
    const { container } = render(
      <AgentMarkdown live={false} text={"Sources: [1], [2, [3\n\nThe second [pass found nothing."} />,
    );

    expect(container.innerHTML).not.toContain("incomplete-link");
    expect(container.textContent).toContain("[2, [3");
    expect(container.textContent).toContain("[pass found nothing.");
  });

  it("keeps a highlighted fence coloured in the commit that settles the text", async () => {
    const { container, rerender } = render(<BubbleWithProbe live />);
    await waitFor(() => expect(isCodeColoured()).toBe(true), { timeout: 10000 });
    colouredAtCommit.length = 0;

    rerender(<BubbleWithProbe live={false} />);

    expect(colouredAtCommit[0]).toBe(true);
    expect(container.innerHTML).not.toContain("incomplete-link");
  });
});
