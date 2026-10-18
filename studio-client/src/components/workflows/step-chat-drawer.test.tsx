import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { MessagePayload } from "@/api/sessions-types";
import type { GraphNode } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { StepChatDrawer } from "./step-chat-drawer";

// The drawer's own network surface: GET /api/sessions/:id/run/steps (the item picker and the
// composer's executing gate) and GET .../run/steps/:node/stream (the live tail), mocked per
// test the same way step-chat.test.ts does. Archived tests hit no network at all -- the archive
// path reads straight off the `recordConversations` prop through `stepConversationToUiMessages`.

const BASE = "http://studio.test";

const NODE: GraphNode = {
  key: "n:draft",
  id: "draft",
  kind: "run",
  signature: "() -> str",
  detail: "",
  tools: [],
  required: true,
  is_output: false,
  unknown: false,
  brought: false,
  briefing: "Write the report",
  work: "draft",
  input_schemas: {},
  output_schema: null,
  source: "",
  turns: null,
};

const MAP_NODE: GraphNode = { ...NODE, kind: "map", id: "review", work: "review" };

// A spec that runs the same step under two node ids: the archive keys by the graph node id,
// never `work`, so this node's own key is `"draft[]"` even though its step is `compose_report`.
const NODE_SHARED_WORK: GraphNode = { ...NODE, work: "compose_report" };

function emptyStreamOf(node: string) {
  return http.get(`${BASE}/api/sessions/s-1/run/steps/${node}/stream`, () =>
    new HttpResponse("data: {\"type\":\"finish\"}\n\ndata: [DONE]\n\n", {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
  );
}

// A stream opening with its own replay-floor frame, `floor`/`requested` set by the caller so a
// test can drive the dropped-output notice on or off, then the given chunks and a close.
function floorStreamOf(node: string, floor: number, requested: number, chunks: Array<Record<string, unknown>>) {
  const body =
    `data: ${JSON.stringify({ type: "data-replay-floor", data: { floor, requested } })}\n\n` +
    chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("") +
    "data: [DONE]\n\n";
  return http.get(`${BASE}/api/sessions/s-1/run/steps/${node}/stream`, () =>
    new HttpResponse(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
  );
}

describe("StepChatDrawer, archived", () => {
  it("renders a user bubble and an assistant turn from a fixture conversation", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [
        { kind: "request", parts: [{ part_kind: "user-prompt", content: "write the report" }] },
        { kind: "response", parts: [{ part_kind: "text", content: "Here is the report." }] },
      ],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={NODE} open onClose={vi.fn()} />,
    );

    expect(await screen.findByText("write the report")).toBeInTheDocument();
    expect(screen.getByText("Here is the report.")).toBeInTheDocument();
  });

  it("looks the archive up by the node id, not by `work`, even when a spec runs the same step under two ids", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [{ kind: "response", parts: [{ part_kind: "text", content: "Here is the report." }] }],
    };

    render(
      <StepChatDrawer
        sessionId={null}
        recordConversations={conversations}
        node={NODE_SHARED_WORK}
        open
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByText("Here is the report.")).toBeInTheDocument();
  });

  it("shows the step's own thinking, which the reused turn component alone would hide", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [
        { kind: "request", parts: [{ part_kind: "user-prompt", content: "go" }] },
        { kind: "response", parts: [{ part_kind: "thinking", content: "I should check the source first." }] },
      ],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={NODE} open onClose={vi.fn()} />,
    );

    expect(await screen.findByText("I should check the source first.")).toBeInTheDocument();
  });

  it("disables the composer with the finished notice (the archive is always read only)", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [{ kind: "response", parts: [{ part_kind: "text", content: "Done." }] }],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={NODE} open onClose={vi.fn()} />,
    );

    await screen.findByText("Done.");
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(
      screen.getByText("This step has finished. Its conversation is read only."),
    ).toBeInTheDocument();
  });

  it("shows an item picker for a node with more than one line, then that item's own conversation", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "review[item-0]": [{ kind: "response", parts: [{ part_kind: "text", content: "Item 0 done." }] }],
      "review[item-1]": [{ kind: "response", parts: [{ part_kind: "text", content: "Item 1 done." }] }],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={MAP_NODE} open onClose={vi.fn()} />,
    );

    expect(screen.getByRole("button", { name: /item-0/ })).toBeInTheDocument();
    expect(screen.queryByText("Item 0 done.")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /item-0/ }));

    expect(await screen.findByText("Item 0 done.")).toBeInTheDocument();
  });

  it("returns to the item picker from a chosen item, and on to a different one", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "review[item-0]": [{ kind: "response", parts: [{ part_kind: "text", content: "Item 0 done." }] }],
      "review[item-1]": [{ kind: "response", parts: [{ part_kind: "text", content: "Item 1 done." }] }],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={MAP_NODE} open onClose={vi.fn()} />,
    );

    await userEvent.click(screen.getByRole("button", { name: /item-0/ }));
    expect(await screen.findByText("Item 0 done.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Choose another item" }));

    expect(screen.getByRole("button", { name: /item-1/ })).toBeInTheDocument();
    expect(screen.queryByText("Item 0 done.")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /item-1/ }));
    expect(await screen.findByText("Item 1 done.")).toBeInTheDocument();
  });

  it("offers no Choose another item control for a node with only one line", async () => {
    const conversations: Record<string, MessagePayload[]> = {
      "draft[]": [{ kind: "response", parts: [{ part_kind: "text", content: "Done." }] }],
    };

    render(
      <StepChatDrawer sessionId={null} recordConversations={conversations} node={NODE} open onClose={vi.fn()} />,
    );

    await screen.findByText("Done.");
    expect(screen.queryByRole("button", { name: "Choose another item" })).not.toBeInTheDocument();
  });
});

describe("StepChatDrawer, live", () => {
  it("disables the composer with the finished notice when no line is executing", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: false, settled: true }] }),
      ),
      emptyStreamOf("draft"),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled());
    expect(
      screen.getByText("This step has finished. Its conversation is read only."),
    ).toBeInTheDocument();
  });

  it("sends a steer with the session, node, item, and typed text", async () => {
    let hitPath = "";
    let body: unknown;
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      emptyStreamOf("draft"),
      http.post(`${BASE}/api/sessions/s-1/run/steps/draft/steer`, async ({ request }) => {
        hitPath = new URL(request.url).pathname + new URL(request.url).search;
        body = await request.json();
        return HttpResponse.json({ ok: true });
      }),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await userEvent.type(textbox, "slow down");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(hitPath).toBe("/api/sessions/s-1/run/steps/draft/steer?item="));
    expect(body).toEqual({ message: "slow down" });
  });

  it("surfaces a 409 steer refusal as the composer notice", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      emptyStreamOf("draft"),
      http.post(`${BASE}/api/sessions/s-1/run/steps/draft/steer`, () =>
        HttpResponse.json({ ok: false, message: "this step has already finished" }, { status: 409 }),
      ),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(textbox).toBeEnabled());
    await userEvent.type(textbox, "slow down");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("this step has already finished")).toBeInTheDocument();
  });

  it("disables the composer with the not-started notice when the node has no line yet", async () => {
    server.use(http.get(`${BASE}/api/sessions/s-1/run/steps`, () => HttpResponse.json({ steps: [] })));

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled());
    expect(screen.getByText("This step has not started yet.")).toBeInTheDocument();
  });

  it("disables the composer with the could-not-read notice when the lines fetch fails", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ ok: false, message: "boom" }, { status: 500 }),
      ),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled());
    expect(screen.getByText("Could not read this step.")).toBeInTheDocument();
  });

  it("labels the bubble with the step, not the orchestrator, and offers no turn-detail button", async () => {
    const chunks = [
      { type: "start" },
      { type: "text-start", id: "t-1" },
      { type: "text-delta", id: "t-1", delta: "Working." },
      { type: "text-end", id: "t-1" },
      { type: "finish" },
    ];
    const body = `${chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("")}data: [DONE]\n\n`;
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      http.get(`${BASE}/api/sessions/s-1/run/steps/draft/stream`, () =>
        new HttpResponse(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
      ),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    expect(await screen.findByText("Working.")).toBeInTheDocument();
    // "Step draft" names both the drawer (its own title) and the bubble (the speaker label) --
    // the second occurrence is the one this test is about.
    expect(screen.getAllByText("Step draft")).toHaveLength(2);
    expect(screen.queryByText("Orchestrator agent")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View turn detail" })).not.toBeInTheDocument();
  });

  it("shows no gate-mode caption -- the drawer steers a step, not a run", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      emptyStreamOf("draft"),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await screen.findByRole("textbox", { name: "Message" });
    expect(screen.queryByText("gates wait for you")).not.toBeInTheDocument();
  });

  it("shows the dropped-output notice above the transcript when the floor is past what it asked for", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      floorStreamOf("draft", 40, 0, [
        { type: "text-start", id: "t-1" },
        { type: "text-delta", id: "t-1", delta: "Working." },
        { type: "text-end", id: "t-1" },
        { type: "finish" },
      ]),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    expect(
      await screen.findByText(
        "Earlier output was dropped from the live buffer. The full conversation is archived when the run ends.",
      ),
    ).toBeInTheDocument();
  });

  it("shows no dropped-output notice when the floor matches what it asked for", async () => {
    server.use(
      http.get(`${BASE}/api/sessions/s-1/run/steps`, () =>
        HttpResponse.json({ steps: [{ node: "draft", item: "", executing: true, settled: false }] }),
      ),
      floorStreamOf("draft", 0, 0, [{ type: "finish" }]),
    );

    render(
      <StepChatDrawer sessionId="s-1" recordConversations={null} node={NODE} open onClose={vi.fn()} />,
    );

    await screen.findByRole("textbox", { name: "Message" });
    expect(screen.queryByText(/Earlier output was dropped/)).not.toBeInTheDocument();
  });
});
