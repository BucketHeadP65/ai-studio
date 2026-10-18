import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { render } from "@/test-support/render";

import { Console } from "./console";

const TURNS = [
  { who: "you" as const, text: "craft a capability" },
  { who: "agent" as const, text: "Authored **summarize_day**, pending your review." },
];

describe("<Console />", () => {
  it("renders the dialogue with the agent's markdown formatted", () => {
    render(
      <Console
        title="The agent"
        turns={TURNS}
        thinking={false}
        onSend={vi.fn()}
        onForget={vi.fn()}
      />,
    );

    expect(screen.getByText("craft a capability")).toBeInTheDocument();
    expect(screen.getByText("summarize_day")).toBeInTheDocument(); // the <strong>, not raw asterisks
  });

  it("shows the empty state only while there is no dialogue", () => {
    const { rerender } = render(
      <Console
        title="The agent"
        turns={[]}
        thinking={false}
        onSend={vi.fn()}
        onForget={vi.fn()}
        emptyState={<p>Ask me to craft something.</p>}
      />,
    );
    expect(screen.getByText("Ask me to craft something.")).toBeInTheDocument();

    rerender(
      <Console
        title="The agent"
        turns={TURNS}
        thinking={false}
        onSend={vi.fn()}
        onForget={vi.fn()}
        emptyState={<p>Ask me to craft something.</p>}
      />,
    );
    expect(screen.queryByText("Ask me to craft something.")).not.toBeInTheDocument();
  });

  it("streams the live turn: the sent text, the prose so far, and the named act", () => {
    render(
      <Console
        title="The agent"
        turns={TURNS}
        thinking
        live={{
          user: "and a script too",
          text: "Writing the script now.",
          act: "write_custom_step",
        }}
        onSend={vi.fn()}
        onForget={vi.fn()}
      />,
    );

    expect(screen.getByText("and a script too")).toBeInTheDocument();
    expect(screen.getByText("Writing the script now.")).toBeInTheDocument();
    expect(screen.getByText("write_custom_step")).toBeInTheDocument();
  });

  it("holds a quiet composing note while the settle streams as raw JSON", () => {
    render(
      <Console
        title="The agent"
        turns={TURNS}
        thinking
        live={{ user: null, text: '{"message": "half a reply', act: null }}
        onSend={vi.fn()}
        onForget={vi.fn()}
      />,
    );

    expect(screen.getByText("writing the reply...")).toBeInTheDocument();
    expect(screen.queryByText(/half a reply/)).not.toBeInTheDocument();
  });

  it("sends on Enter and clears, keeping Shift+Enter for a newline", async () => {
    const onSend = vi.fn();
    render(
      <Console title="The agent" turns={[]} thinking={false} onSend={onSend} onForget={vi.fn()} />,
    );

    const box = screen.getByRole("textbox", { name: "Message" });
    await userEvent.type(box, "hello{Enter}");
    expect(onSend).toHaveBeenCalledWith("hello");
    expect(box).toHaveValue("");
    await userEvent.type(box, "line{Shift>}{Enter}{/Shift}two");
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("surfaces a stream failure as an alert", () => {
    render(
      <Console
        title="The agent"
        turns={TURNS}
        thinking={false}
        error="the stream closed without settling the turn"
        onSend={vi.fn()}
        onForget={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("without settling");
  });

  it("offers Forget only once there is something to forget", () => {
    const onForget = vi.fn();
    const { rerender } = render(
      <Console
        title="The agent"
        turns={[]}
        thinking={false}
        onSend={vi.fn()}
        onForget={onForget}
      />,
    );
    expect(screen.queryByRole("button", { name: "Forget" })).not.toBeInTheDocument();

    rerender(
      <Console
        title="The agent"
        turns={TURNS}
        thinking={false}
        onSend={vi.fn()}
        onForget={onForget}
      />,
    );
    expect(screen.getByRole("button", { name: "Forget" })).toBeInTheDocument();
  });
  it("seeds the composer so a scoped ask arrives ready to edit", () => {
    render(
      <Console
        title="The agent"
        turns={[]}
        thinking={false}
        onSend={vi.fn()}
        onForget={vi.fn()}
        seed="About the capability summarize_day: "
      />,
    );

    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue(
      "About the capability summarize_day: ",
    );
  });
});
