import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import type { UIMessage } from "ai";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { sessionKeys } from "@/api/query-keys";
import type { TranscriptMessage } from "@/api/sessions-types";
import { render } from "@/shared/test-support/render";

import { TurnInspector } from "./turn-inspector";

const HUGE_RETURN = "skill body line\n".repeat(1000);

function seededQueryClient(sessionId: string, transcript: TranscriptMessage[]): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(sessionKeys.transcript(sessionId), transcript);
  return queryClient;
}

function renderInspector(
  queryClient: QueryClient,
  props: { sessionId: string; message: UIMessage; onClose: () => void },
): ReactElement {
  return (
    <QueryClientProvider client={queryClient}>
      <TurnInspector {...props} />
    </QueryClientProvider>
  );
}

const STORED_RUN: TranscriptMessage[] = [
  {
    run_id: "r1",
    idx: 0,
    kind: "request",
    payload: {
      kind: "request",
      parts: [{ part_kind: "user-prompt", content: "load the docs skill" }],
    },
    text: "load the docs skill",
  },
  {
    run_id: "r1",
    idx: 1,
    kind: "response",
    payload: {
      kind: "response",
      parts: [{ part_kind: "tool-call", tool_name: "load_skill", args: { name: "docs" } }],
    },
    text: "",
  },
  {
    run_id: "r1",
    idx: 2,
    kind: "request",
    payload: {
      kind: "request",
      parts: [{ part_kind: "tool-return", tool_name: "load_skill", content: HUGE_RETURN }],
    },
    text: "",
  },
  {
    run_id: "r1",
    idx: 3,
    kind: "response",
    payload: {
      kind: "response",
      parts: [{ part_kind: "text", content: "Loaded the docs skill." }],
    },
    text: "Loaded the docs skill.",
  },
];

describe("TurnInspector", () => {
  it("renders one row per part of a stored turn, from the last user prompt through the reply", () => {
    const queryClient = seededQueryClient("s1", STORED_RUN);
    render(
      renderInspector(queryClient, {
        sessionId: "s1",
        message: {
          id: "r1:3",
          role: "assistant",
          parts: [{ type: "text", text: "Loaded the docs skill." }],
        },
        onClose: vi.fn(),
      }),
    );

    expect(screen.getByText("load the docs skill")).toBeInTheDocument();
    expect(screen.getAllByText("load_skill")).toHaveLength(2); // one badge for the call, one for the return
    expect(screen.getByText("call")).toBeInTheDocument();
    expect(screen.getByText("return")).toBeInTheDocument();
    expect(screen.getByText('"docs"')).toBeInTheDocument(); // rendered inside the args' JsonTree
    expect(screen.getByText("Loaded the docs skill.")).toBeInTheDocument();
  });

  it("stays open, and stays the person's, while a value is viewed inside it", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      renderInspector(seededQueryClient("s1", STORED_RUN), {
        sessionId: "s1",
        message: {
          id: "r1:3",
          role: "assistant",
          parts: [{ type: "text", text: "Loaded the docs skill." }],
        },
        onClose,
      }),
    );

    await user.click(screen.getByRole("button", { name: "View" }));
    const viewer = await screen.findByRole("dialog", { name: "load_skill return" });
    // The modal viewer hides the page behind it, the inspector included, from assistive
    // tech while it is open (its name reads empty then); the inspector is still mounted
    // underneath as the second dialog.
    expect(screen.getAllByRole("dialog", { hidden: true })).toHaveLength(2);
    expect(onClose).not.toHaveBeenCalled();

    await user.click(within(viewer).getByRole("button", { name: "Close drawer" }));

    expect(screen.queryByRole("dialog", { name: "load_skill return" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Turn detail" })).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("truncates a huge tool return instead of rendering it whole, and opens it in plain mode on view", async () => {
    const user = userEvent.setup();
    const queryClient = seededQueryClient("s1", STORED_RUN);
    render(
      renderInspector(queryClient, {
        sessionId: "s1",
        message: {
          id: "r1:3",
          role: "assistant",
          parts: [{ type: "text", text: "Loaded the docs skill." }],
        },
        onClose: vi.fn(),
      }),
    );

    expect(screen.queryByText(HUGE_RETURN)).not.toBeInTheDocument();
    const viewButton = screen.getByRole("button", { name: "View" });

    await user.click(viewButton);

    const viewer = await screen.findByRole("dialog", { name: "load_skill return" });
    expect(within(viewer).getByText("load_skill return")).toBeInTheDocument();
    // Plain mode: a pre block, verbatim, with every newline intact -- not markdown, which
    // would fold the return's own line breaks into one paragraph.
    const pre = viewer.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toBe(HUGE_RETURN);
  });

  it("opens a tool-call's own arguments in plain mode too", async () => {
    const user = userEvent.setup();
    const bigArgs = { name: "docs", note: "line one\nline two\n".repeat(50) };
    const run: TranscriptMessage[] = [
      {
        run_id: "r2",
        idx: 0,
        kind: "request",
        payload: {
          kind: "request",
          parts: [{ part_kind: "user-prompt", content: "load the docs skill" }],
        },
        text: "load the docs skill",
      },
      {
        run_id: "r2",
        idx: 1,
        kind: "response",
        payload: {
          kind: "response",
          parts: [{ part_kind: "tool-call", tool_name: "load_skill", args: bigArgs }],
        },
        text: "",
      },
    ];
    const queryClient = seededQueryClient("s1", run);
    render(
      renderInspector(queryClient, {
        sessionId: "s1",
        message: { id: "r2:1", role: "assistant", parts: [{ type: "text", text: "" }] },
        onClose: vi.fn(),
      }),
    );

    await user.click(screen.getByRole("button", { name: "View" }));

    const viewer = await screen.findByRole("dialog", { name: "load_skill args" });
    const pre = viewer.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toContain("line one\nline two");
  });

  it("keeps rendering the model's own text as markdown, distinct from the plain payload rows", () => {
    const run: TranscriptMessage[] = [
      {
        run_id: "r3",
        idx: 0,
        kind: "request",
        payload: {
          kind: "request",
          parts: [{ part_kind: "user-prompt", content: "say something" }],
        },
        text: "say something",
      },
      {
        run_id: "r3",
        idx: 1,
        kind: "response",
        payload: {
          kind: "response",
          parts: [{ part_kind: "text", content: "here is a **bold** word" }],
        },
        text: "here is a **bold** word",
      },
    ];
    const queryClient = seededQueryClient("s1", run);
    render(
      renderInspector(queryClient, {
        sessionId: "s1",
        message: {
          id: "r3:1",
          role: "assistant",
          parts: [{ type: "text", text: "here is a **bold** word" }],
        },
        onClose: vi.fn(),
      }),
    );

    const bold = screen.getByText("bold");
    expect(bold.tagName).toBe("STRONG");
  });

  it("walks a turn the person answered mid-way across the run it resumed on", () => {
    const resumed: TranscriptMessage[] = [
      ...STORED_RUN,
      {
        run_id: "r9",
        idx: 0,
        kind: "request",
        payload: {
          kind: "request",
          parts: [{ part_kind: "tool-return", tool_name: "set_tab", content: "switched" }],
        },
        text: "",
      },
      {
        run_id: "r9",
        idx: 1,
        kind: "response",
        payload: { kind: "response", parts: [{ part_kind: "text", content: "Tab switched." }] },
        text: "Tab switched.",
      },
    ];
    const queryClient = seededQueryClient("s1", resumed);
    render(
      renderInspector(queryClient, {
        sessionId: "s1",
        message: {
          id: "r9:1",
          role: "assistant",
          parts: [{ type: "text", text: "Tab switched." }],
        },
        onClose: vi.fn(),
      }),
    );

    expect(screen.getByText("load the docs skill")).toBeInTheDocument();
    expect(screen.getByText("Loaded the docs skill.")).toBeInTheDocument();
    expect(screen.getByText("Tab switched.")).toBeInTheDocument();
  });

  it("starts an idx before any user-prompt at the run's own first message", () => {
    const queryClient = seededQueryClient("s1", STORED_RUN);
    render(
      renderInspector(queryClient, {
        sessionId: "s1",
        message: { id: "r1:1", role: "assistant", parts: [{ type: "text", text: "" }] },
        onClose: vi.fn(),
      }),
    );

    // idx 1 (the tool-call) has no user-prompt at or before it other than idx 0's.
    expect(screen.getByText("load the docs skill")).toBeInTheDocument();
    expect(screen.getByText("call")).toBeInTheDocument();
    expect(screen.queryByText("return")).not.toBeInTheDocument();
  });

  it("falls back to the live message's own parts when the id isn't in the cached transcript", () => {
    const queryClient = seededQueryClient("s1", STORED_RUN);
    const liveMessage: UIMessage = {
      id: "live-1",
      role: "assistant",
      parts: [
        { type: "text", text: "Working on it." },
        {
          type: "dynamic-tool",
          toolName: "search",
          toolCallId: "call-1",
          state: "output-available",
          input: { query: "docs" },
          output: "three results",
        },
      ],
    };

    render(
      renderInspector(queryClient, { sessionId: "s1", message: liveMessage, onClose: vi.fn() }),
    );

    expect(screen.getByText("Working on it.")).toBeInTheDocument();
    expect(screen.getAllByText("search")).toHaveLength(2); // one badge for the call, one for the return
    expect(screen.getByText("call")).toBeInTheDocument();
    expect(screen.getByText("return")).toBeInTheDocument();
    expect(screen.getByText("three results")).toBeInTheDocument();
  });

  it("closes on the drawer's own close button", async () => {
    const user = userEvent.setup();
    const queryClient = seededQueryClient("s1", STORED_RUN);
    const onClose = vi.fn();
    render(
      renderInspector(queryClient, {
        sessionId: "s1",
        message: {
          id: "r1:3",
          role: "assistant",
          parts: [{ type: "text", text: "Loaded the docs skill." }],
        },
        onClose,
      }),
    );

    await user.click(screen.getByRole("button", { name: "Close drawer" }));

    expect(onClose).toHaveBeenCalledOnce();
  });
});
