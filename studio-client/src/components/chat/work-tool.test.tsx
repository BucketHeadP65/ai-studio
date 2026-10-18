import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { WorkTool } from "./work-tool";

import type { ToolUIPart } from "ai";

function readFileOutput(path: string, lines: string[]): string {
  const numbered = lines.map((line, index) => `${index + 1}\t${line}`).join("\n");
  return `[${path} | ${lines.length} lines | hash:abc123]\n${numbered}\n`;
}

const READ_FILE: ToolUIPart = {
  type: "tool-read_file",
  toolCallId: "c-1",
  state: "output-available",
  input: { path: "docs/plan.md" },
  output: "# the plan",
};

describe("WorkTool", () => {
  it("names the tool and where it stands on the closed card", () => {
    render(<WorkTool part={READ_FILE} facts={undefined} />);

    expect(screen.getByText(/read_file/)).toBeInTheDocument();
    expect(screen.getByLabelText("Completed")).toBeInTheDocument();
    expect(screen.queryByText("Completed")).not.toBeInTheDocument();
  });

  it("keeps the closed card to one row: the header control carries the summary", () => {
    render(
      <WorkTool
        part={{
          type: "tool-read_file",
          toolCallId: "c-13",
          state: "output-available",
          input: { path: "docs/plan.md" },
          output: readFileOutput("docs/plan.md", ["one", "two", "three"]),
        }}
        facts={undefined}
      />,
    );

    const header = screen.getByRole("button", { name: /Read docs\/plan\.md/ });
    expect(within(header).getByText("3 lines")).toBeInTheDocument();
    expect(within(header).getByText("read_file")).toBeInTheDocument();
  });

  it("says so in words while a call is still running", () => {
    render(
      <WorkTool
        part={{
          type: "tool-read_file",
          toolCallId: "c-14",
          state: "input-available",
          input: { path: "docs/plan.md" },
        }}
        facts={undefined}
      />,
    );

    expect(screen.getByText(/^Running/)).toBeInTheDocument();
  });

  it("ticks the running status off its own clock as the card ages", () => {
    vi.useFakeTimers();
    try {
      render(
        <WorkTool
          part={{
            type: "tool-read_file",
            toolCallId: "c-20",
            state: "input-available",
            input: { path: "docs/plan.md" },
          }}
          facts={undefined}
        />,
      );

      expect(screen.getByText("Running 0s")).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(4000);
      });

      expect(screen.getByText("Running 4s")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows a settled call's duration and tokens from facts, after the summary", () => {
    render(
      <WorkTool
        part={{
          type: "tool-read_file",
          toolCallId: "c-21",
          state: "output-available",
          input: { path: "docs/plan.md" },
          output: readFileOutput("docs/plan.md", ["one"]),
        }}
        facts={{
          tokensOut: 51,
          startedAt: "2026-08-28T23:00:00.000Z",
          returnedAt: "2026-08-28T23:00:00.700Z",
        }}
      />,
    );

    expect(screen.getByText("1 line")).toBeInTheDocument();
    expect(screen.getByText("(0.7s, ↓ 51 tokens)")).toBeInTheDocument();
  });

  it("shows tokens alone when facts give no returned timestamp to time the call by", () => {
    render(
      <WorkTool
        part={{
          type: "tool-read_file",
          toolCallId: "c-22",
          state: "output-available",
          input: {},
          output: "x",
        }}
        facts={{ tokensOut: 51, startedAt: "2026-08-28T23:00:00.000Z", returnedAt: null }}
      />,
    );

    expect(screen.getByText("(↓ 51 tokens)")).toBeInTheDocument();
  });

  it("times a call by its own clock when it watched the call finish live, with no facts", () => {
    vi.useFakeTimers();
    try {
      const { rerender } = render(
        <WorkTool
          part={{
            type: "tool-read_file",
            toolCallId: "c-23",
            state: "input-available",
            input: { path: "a.md" },
          }}
          facts={undefined}
        />,
      );

      act(() => {
        vi.advanceTimersByTime(1500);
      });

      rerender(
        <WorkTool
          part={{
            type: "tool-read_file",
            toolCallId: "c-23",
            state: "output-available",
            input: { path: "a.md" },
            output: "hi",
          }}
          facts={undefined}
        />,
      );

      expect(screen.getByText("(1.5s)")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows no parenthetical detail when a card mounts already settled with no facts", () => {
    render(
      <WorkTool
        part={{
          type: "tool-read_file",
          toolCallId: "c-24",
          state: "output-available",
          input: {},
          output: "x",
        }}
        facts={undefined}
      />,
    );

    expect(screen.queryByText(/^\(/)).not.toBeInTheDocument();
  });

  it("reads the header as a verb plus subject, with the raw name still findable", () => {
    render(<WorkTool part={READ_FILE} facts={undefined} />);

    expect(screen.getByText(/Read docs\/plan\.md/)).toBeInTheDocument();
    expect(screen.getByText(/read_file/)).toBeInTheDocument();
  });

  it("shows a completed call's result summary on the closed line", () => {
    render(
      <WorkTool
        part={{
          type: "tool-read_file",
          toolCallId: "c-9",
          state: "output-available",
          input: { path: "docs/plan.md" },
          output: readFileOutput("docs/plan.md", ["one", "two", "three"]),
        }}
        facts={undefined}
      />,
    );

    expect(screen.getByText("3 lines")).toBeInTheDocument();
  });

  it("shows an unknown tool's raw name as its verb, without repeating it", () => {
    render(
      <WorkTool
        part={{
          type: "tool-frobnicate",
          toolCallId: "c-10",
          state: "output-available",
          input: {},
          output: "hi",
        }}
        facts={undefined}
      />,
    );

    expect(screen.getAllByText("frobnicate")).toHaveLength(1);
  });

  it("renders an edit_file call's changes as a diff, with the path visible", async () => {
    const user = userEvent.setup();
    render(
      <WorkTool
        part={{
          type: "tool-edit_file",
          toolCallId: "c-11",
          state: "output-available",
          input: { path: "docs/plan.md", old_text: "a\nb\n", new_text: "a\nB\n" },
          output: "ok",
        }}
        facts={undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: /edit_file/ }));

    expect(await screen.findByText("docs/plan.md")).toBeInTheDocument();
    expect(screen.getByText("b")).toBeInTheDocument();
    expect(screen.getByText("B")).toBeInTheDocument();
  });

  it("renders a write_file call's content with a line count", async () => {
    const user = userEvent.setup();
    render(
      <WorkTool
        part={{
          type: "tool-write_file",
          toolCallId: "c-12",
          state: "output-available",
          input: { path: "docs/plan.md", content: "one\ntwo" },
          output: "ok",
        }}
        facts={undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: /write_file/ }));

    expect(await screen.findByText(/one/)).toBeInTheDocument();
    expect(screen.getAllByText("2 lines")).toHaveLength(2);
  });

  it("reads a find_files call's header and summary in the search style", () => {
    render(
      <WorkTool
        part={{
          type: "tool-find_files",
          toolCallId: "c-15",
          state: "output-available",
          input: { pattern: "*.py" },
          output: "a.py\nb.py",
        }}
        facts={undefined}
      />,
    );

    expect(screen.getByText(/Found files "\*\.py"/)).toBeInTheDocument();
    expect(screen.getByText("2 matches")).toBeInTheDocument();
  });

  it("opens to the call's parameters and result", async () => {
    const user = userEvent.setup();
    render(<WorkTool part={READ_FILE} facts={undefined} />);

    await user.click(screen.getByRole("button", { name: /read_file/ }));

    expect(await screen.findByText(/"docs\/plan\.md"/)).toBeInTheDocument();
    expect(screen.getByText(/# the plan/)).toBeInTheDocument();
  });

  it("shows a string result as wrapped text inside a capped scroll region", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <WorkTool
        part={{
          type: "tool-run_command",
          toolCallId: "c-5",
          state: "output-available",
          input: { cmd: "ls" },
          output: "line one\nline two",
        }}
        facts={undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: /run_command/ }));

    const result = await screen.findByText(/line one/);
    expect(result.className).toContain("whitespace-pre-wrap");
    const region = container.querySelector(".overflow-y-auto");
    expect(region?.className).toMatch(/max-h-/);
    expect(region?.contains(result)).toBe(true);
  });

  it("shows an object result as a tree of its keys", async () => {
    const user = userEvent.setup();
    render(
      <WorkTool
        part={{
          type: "tool-search",
          toolCallId: "c-6",
          state: "output-available",
          input: { q: "plan" },
          output: { hits: 3, top: "docs/plan.md" },
        }}
        facts={undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: /search/ }));

    expect(await screen.findByText("hits")).toBeInTheDocument();
    expect(screen.getByText("top")).toBeInTheDocument();
  });

  it("opens a long string result in the viewer", async () => {
    const user = userEvent.setup();
    render(
      <WorkTool
        part={{
          type: "tool-fetch",
          toolCallId: "c-8",
          state: "output-available",
          input: { url: "https://example.test" },
          output: "y".repeat(400),
        }}
        facts={undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: /fetch/ }));
    await user.click(await screen.findByRole("button", { name: "View" }));

    expect(await screen.findByRole("dialog", { name: /fetch/ })).toBeInTheDocument();
  });

  it("survives opening while the call's arguments are still streaming", async () => {
    const user = userEvent.setup();
    render(
      <WorkTool
        part={{
          type: "tool-read_file",
          toolCallId: "c-3",
          state: "input-streaming",
          input: undefined,
        }}
        facts={undefined}
      />,
    );

    await user.click(screen.getByRole("button", { name: /read_file/ }));

    expect(await screen.findByText("Parameters")).toBeInTheDocument();
  });

  it("names a dynamic tool from its own toolName", () => {
    render(
      <WorkTool
        part={{
          type: "dynamic-tool",
          toolName: "search_notes",
          toolCallId: "c-4",
          state: "input-available",
          input: { q: "plan" },
        }}
        facts={undefined}
      />,
    );

    expect(screen.getByText("search_notes")).toBeInTheDocument();
  });

  it("shows a failed call's error text", async () => {
    const user = userEvent.setup();
    render(
      <WorkTool
        part={{
          type: "tool-read_file",
          toolCallId: "c-2",
          state: "output-error",
          input: { path: "docs/plan.md" },
          errorText: "no such file",
        }}
        facts={undefined}
      />,
    );

    expect(screen.getByText("Error")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /read_file/ }));
    expect(await screen.findByText("no such file")).toBeInTheDocument();
  });
});
