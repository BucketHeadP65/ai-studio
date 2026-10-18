import { screen } from "@testing-library/dom";
import { act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { RunView, SessionSummary } from "@/api/sessions-types";
import type { GateMode, RunState, WorkflowDetail } from "@/api/studio-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

vi.mock("next/navigation", () => ({
  usePathname: () => "/chat",
  useSearchParams: () => new URLSearchParams(),
}));

// Imported after the mock so the components under test see the mocked module.
import type { FollowMe } from "@/components/follow-me/provider";
import { FollowMeProvider, useFollowMe } from "@/components/follow-me/provider";

import { SessionChat } from "./session-chat";

let followMe: FollowMe = {
  collectContext: () => {
    throw new Error("Probe did not mount");
  },
  registerExtras: () => () => {},
  registerTarget: () => () => {},
  findTarget: () => null,
  targetNames: () => [],
  whenAt: () => Promise.resolve(),
};

function Probe(): null {
  followMe = useFollowMe();
  return null;
}

// SessionChat's own network surface, wherever it mounts: the transcript for its session, the
// session list (for `answering`), the runs list (for the right rail's own gate turn and gate
// mode), and run state (the gate's own elapsed event, and the live gate mode). Every request the
// component can fire is mocked here, matching the rest of the suite's convention.

const BASE = "http://studio.test";

function usage() {
  return { input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 };
}

function userPromptPayload(text: string) {
  return { kind: "request", parts: [{ part_kind: "user-prompt", content: text }] };
}

function assistantTextPayload(text: string) {
  return { kind: "response", parts: [{ part_kind: "text", content: text }] };
}

function sessionFixture(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    id: "s1",
    project_id: "proj-1",
    title: "A session",
    started_at: "2026-08-23T10:00:00Z",
    last_activity_at: "2026-08-23T10:00:00Z",
    archived: false,
    answering: false,
    turn_count: 1,
    usage: usage(),
    kind: "chat",
    cost_usd: "0",
    unpriced: 0,
    context: null,
    ...overrides,
  };
}

function runFixture(overrides: Partial<RunView> = {}): RunView {
  return {
    session_id: "s1",
    workflow_id: "wf.demo",
    run_title: "",
    state: "running",
    active: [],
    done: [],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: "2026-08-24T00:00:00.000Z",
    ...overrides,
  };
}

function runStateFixture(overrides: Partial<RunState> = {}): RunState {
  return {
    running: true,
    workflow: "wf.demo",
    result: null,
    active: [],
    done: [],
    failed: [],
    waiting_at: null,
    events: [],
    seq: 1,
    auto_approve: false,
    gate_mode: "wait",
    ...overrides,
  };
}

const GATE = {
  gate_id: "gate-9",
  workflow: "wf.demo",
  reviewing: "{}",
  rejection_reruns: "compose",
};

function workflowFixture(): WorkflowDetail {
  return {
    ok: true,
    id: "wf.demo",
    name: "demo",
    version: 1,
    description: "",
    title: "Demo",
    summary: "",
    applicability: "",
    provisional: false,
    graph: { name: "demo", version: 1, nodes: [], edges: [] },
    issues: [],
    fits: true,
    missing: [],
    brings: [],
    spec_yaml: "",
    staleness: [],
    lineage: null,
    birth: [],
    carried: [],
    predictions: null,
    prediction_record: null,
    trust: {
      level: "unverified",
      runs_with_evidence: 0,
      failed_runs: 0,
      approved_by: [],
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
    },
    archived_at: null,
  };
}

function mountApi(
  overrides: {
    transcript?: Record<string, unknown>[];
    transcriptStatus?: number;
    sessions?: SessionSummary[];
    session?: SessionSummary;
    runs?: RunView[];
    runState?: RunState;
    workflow?: WorkflowDetail;
  } = {},
) {
  server.use(
    http.get(`${BASE}/api/sessions/:id/transcript`, () => {
      if (overrides.transcriptStatus) {
        return HttpResponse.json(
          { detail: "the session was not found" },
          { status: overrides.transcriptStatus },
        );
      }
      return HttpResponse.json({ messages: overrides.transcript ?? [] });
    }),
    http.get(`${BASE}/api/sessions`, () =>
      HttpResponse.json({ sessions: overrides.sessions ?? [] }),
    ),
    http.get(`${BASE}/api/sessions/:id`, () =>
      HttpResponse.json({ session: overrides.session ?? sessionFixture(), turns: [] }),
    ),
    http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: overrides.runs ?? [] })),
    http.get(`${BASE}/api/sessions/:id/run/state`, () =>
      HttpResponse.json(overrides.runState ?? runStateFixture()),
    ),
    http.get(`${BASE}/api/workflow/:id`, () =>
      HttpResponse.json(overrides.workflow ?? workflowFixture()),
    ),
    // Every send knocks on the steer endpoint first; an idle session answers 409 and the message
    // goes out as an ordinary turn.
    http.post(`${BASE}/api/sessions/:id/steer`, () =>
      HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 }),
    ),
  );
}

// The chat stream in the protocol the engine reads: one `data:` line per chunk, closed by
// `data: [DONE]`.
function streamResponse(chunks: Record<string, unknown>[]) {
  const lines = chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("");
  return new HttpResponse(`${lines}data: [DONE]\n\n`, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

// A turn that ends waiting on the person: one console act announced, its approval outstanding.
const APPROVAL_TURN: Record<string, unknown>[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-input-start", toolCallId: "c-2", toolName: "toggle_plugin" },
  {
    type: "tool-input-available",
    toolCallId: "c-2",
    toolName: "toggle_plugin",
    input: { name: "sql", on: true },
  },
  { type: "tool-approval-request", approvalId: "c-2", toolCallId: "c-2" },
  { type: "finish-step" },
  { type: "finish" },
];

function renderChat(gateMode: GateMode = "wait", onChangeGates = vi.fn()) {
  return render(
    <SessionChat
      sessionId="s1"
      reply={null}
      gateMode={gateMode}
      onChangeGates={onChangeGates}
      onSettle={vi.fn()}
    />,
  );
}

describe("SessionChat", () => {
  it("shows a loading state before the transcript resolves", () => {
    mountApi();
    renderChat();

    expect(screen.getByText("Loading the conversation.")).toBeInTheDocument();
  });

  it("renders the transcript and the composer once it resolves", async () => {
    mountApi({
      transcript: [
        { run_id: "r1", idx: 0, kind: "request", payload: userPromptPayload("Hi"), text: "Hi" },
        {
          run_id: "r1",
          idx: 1,
          kind: "response",
          payload: assistantTextPayload("Hello there"),
          text: "Hello there",
        },
      ],
    });

    renderChat();

    expect(await screen.findByText("Hello there")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
  });

  it("shows the turns that landed while the chat was unmounted, without a reload", async () => {
    const before = [
      { run_id: "r1", idx: 0, kind: "request", payload: userPromptPayload("Hi"), text: "Hi" },
      {
        run_id: "r1",
        idx: 1,
        kind: "response",
        payload: assistantTextPayload("Hello there"),
        text: "Hello there",
      },
    ];
    mountApi({ transcript: before });
    const { rerender } = render(
      <SessionChat
        sessionId="s1"
        reply={null}
        gateMode="wait"
        onChangeGates={vi.fn()}
        onSettle={vi.fn()}
      />,
    );
    expect(await screen.findByText("Hello there")).toBeInTheDocument();

    // The reader leaves for a workflow page (the chat unmounts) and a new turn
    // lands on the server meanwhile; coming back must show it on its own.
    rerender(<div />);
    mountApi({
      transcript: [
        ...before,
        {
          run_id: "r2",
          idx: 0,
          kind: "response",
          payload: assistantTextPayload("Back with news"),
          text: "Back with news",
        },
      ],
    });
    rerender(
      <SessionChat
        sessionId="s1"
        reply={null}
        gateMode="wait"
        onChangeGates={vi.fn()}
        onSettle={vi.fn()}
      />,
    );

    expect(await screen.findByText("Back with news")).toBeInTheDocument();
  });

  it("shows an error state when the transcript cannot be reached", async () => {
    mountApi({ transcriptStatus: 404 });

    renderChat();

    expect(await screen.findByText(/the session was not found/)).toBeInTheDocument();
  });

  it("keeps the composer open while this session is answering, beside the Stop button", async () => {
    // A send while the turn runs steers it through the steer endpoint instead of starting a
    // second turn, so an answering session no longer locks the box.
    mountApi({ sessions: [sessionFixture({ answering: true })] });

    renderChat();

    await screen.findByRole("textbox", { name: "Message" });
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("keeps a waiting act answerable however stale the session list's answering flag is", async () => {
    mountApi({ sessions: [sessionFixture({ answering: true })] });
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, () => streamResponse(APPROVAL_TURN)),
    );

    render(
      <SessionChat
        sessionId="s1"
        reply={null}
        gateMode="wait"
        onChangeGates={vi.fn()}
        onSettle={vi.fn()}
        initialSend="turn on the sql plugin"
      />,
    );

    expect(await screen.findByRole("button", { name: "Approve toggle_plugin" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Deny toggle_plugin" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reply instead" })).toBeEnabled();
  });

  it("shows the gate turn from this session's own waiting run, and decide posts to it", async () => {
    mountApi({ runs: [runFixture({ state: "waiting", waiting_at: "gate-9", gate: GATE })] });
    let decidedBody: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/s1/decide`, async ({ request }) => {
        decidedBody = await request.json();
        return HttpResponse.json({ ok: true, approved: true });
      }),
    );

    renderChat();

    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));

    expect(decidedBody).toEqual({ approve: true });
  });

  it("keeps the composer usable while this session's own run waits at a gate", async () => {
    mountApi({
      sessions: [sessionFixture({ answering: true })],
      runs: [runFixture({ state: "waiting", waiting_at: "gate-9", gate: GATE })],
    });

    renderChat();

    await screen.findByRole("button", { name: "Approve" });
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeInTheDocument();
  });

  it("opens the turn inspector on an assistant bubble click", async () => {
    mountApi({
      transcript: [
        {
          run_id: "r1",
          idx: 0,
          kind: "request",
          payload: userPromptPayload("Hi"),
          text: "Hi",
        },
        {
          run_id: "r1",
          idx: 1,
          kind: "response",
          payload: assistantTextPayload("Hello there"),
          text: "Hello there",
        },
      ],
    });

    renderChat();
    await screen.findByText("Hello there");

    await userEvent.click(screen.getByRole("button", { name: "View turn detail" }));

    expect(await screen.findByRole("dialog", { name: "Turn detail" })).toBeInTheDocument();
  });

  it("shows this session's own live run as a turn, named off its workflow", async () => {
    mountApi({
      session: sessionFixture({ title: "Literature check" }),
      runs: [runFixture()],
    });

    renderChat();

    expect(await screen.findByText("Run")).toBeInTheDocument();
    expect(await screen.findByText("Demo")).toBeInTheDocument();
    expect(screen.getByText("wf.demo")).toBeInTheDocument();
  });

  it("shows no run turn when this session has no live run", async () => {
    mountApi();

    renderChat();
    await screen.findByRole("textbox", { name: "Message" });

    expect(screen.queryByText("Run")).not.toBeInTheDocument();
  });

  it("keeps the composer a target while the approval card holds its place", async () => {
    mountApi();
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, () => streamResponse(APPROVAL_TURN)),
    );

    render(
      <FollowMeProvider>
        <Probe />
        <SessionChat
          sessionId="s1"
          reply={null}
          gateMode="wait"
          onChangeGates={vi.fn()}
          onSettle={vi.fn()}
          initialSend="turn on the sql plugin"
          composerTarget="composer"
        />
      </FollowMeProvider>,
    );

    await screen.findByRole("button", { name: "Approve toggle_plugin" });
    expect(screen.queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
    expect(followMe.targetNames()).toEqual(["composer"]);

    act(() => {
      followMe.findTarget("composer")?.handle.prefill?.("ask it another way");
    });
    await userEvent.click(screen.getByRole("button", { name: "Reply instead" }));

    expect(await screen.findByRole("textbox", { name: "Message" })).toHaveValue(
      "ask it another way",
    );
  });

  it("says so in the transcript when a companion act was left waiting and never ran", async () => {
    mountApi({
      transcript: [
        {
          run_id: "r1",
          idx: 0,
          kind: "request",
          payload: userPromptPayload("turn on the sql plugin"),
          text: "turn on the sql plugin",
        },
        {
          run_id: "r1",
          idx: 1,
          kind: "response",
          payload: {
            kind: "response",
            parts: [
              {
                part_kind: "tool-call",
                tool_name: "toggle_plugin",
                tool_call_id: "c-2",
                args: { name: "sql", open: true },
              },
            ],
          },
          text: "",
        },
      ],
    });

    renderChat();

    expect(await screen.findByText("toggle_plugin, left waiting and never ran")).toBeInTheDocument();
  });
});
