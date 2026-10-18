import { screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DraftProvider } from "@/api/draft-store";
import type { PluginsListing, TiersListing } from "@/api/harness-types";
import type { RunView, SessionSummary } from "@/api/sessions-types";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import ChatPage from "./page";

// The chat station composes every finished part behind one shell: sessions on the left, the
// running conversation in the center, this session's own status row under the composer. Every
// request the page's own mount can fire is mocked here (MSW errors on anything unhandled), so
// this test also pins the page's network surface: sessions/projects/transcript for the left
// column, runs/run-state/workflow for the statusline and the gate turn, harness/tiers and
// harness/plugins for the composer's model word and the plugins count.

let searchParamsValue = new URLSearchParams();
const routerReplace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: routerReplace, push: vi.fn() }),
  useSearchParams: () => searchParamsValue,
}));

afterEach(() => {
  searchParamsValue = new URLSearchParams();
  routerReplace.mockClear();
  // The rails remember their fold per browser; one test's fold must not reach the next.
  window.localStorage.clear();
});

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

const STORED_SESSION: SessionSummary = {
  id: "s1",
  project_id: "proj-1",
  title: "Stored chat",
  started_at: "2026-08-20T10:00:00Z",
  last_activity_at: "2026-08-20T10:00:00Z",
  archived: false,
  answering: false,
  turn_count: 2,
  usage: usage(),
  kind: "chat",
  cost_usd: "0",
  unpriced: 0,
  context: null,
};

const ANSWERING_SESSION: SessionSummary = {
  id: "s2",
  project_id: "proj-1",
  title: "Answering chat",
  started_at: "2026-08-21T10:00:00Z",
  last_activity_at: "2026-08-21T10:00:00Z",
  archived: false,
  answering: true,
  turn_count: 1,
  usage: usage(),
  kind: "chat",
  cost_usd: "0",
  unpriced: 0,
  context: null,
};

const NEW_SESSION: SessionSummary = {
  id: "s-new",
  project_id: "proj-1",
  title: "session s-new",
  started_at: "2026-08-23T10:00:00Z",
  last_activity_at: "2026-08-23T10:00:00Z",
  archived: false,
  answering: false,
  turn_count: 0,
  usage: usage(),
  kind: "chat",
  cost_usd: "0",
  unpriced: 0,
  context: null,
};

const TOOL_SESSION: SessionSummary = {
  id: "s3",
  project_id: "proj-1",
  title: "Tool chat",
  started_at: "2026-08-22T10:00:00Z",
  last_activity_at: "2026-08-22T10:00:00Z",
  archived: false,
  answering: false,
  turn_count: 1,
  usage: usage(),
  kind: "chat",
  cost_usd: "0",
  unpriced: 0,
  context: null,
};

const HUGE_TOOL_RETURN = "skill body line\n".repeat(1000);

const PROJECTS = [{ id: "proj-1", name: "Engram", root_path: "/repo/engram" }];

const GATE = {
  gate_id: "gate-9",
  workflow: "wf.demo",
  reviewing: "{}",
  rejection_reruns: "compose",
};

const WAITING_RUN: RunView = {
  session_id: "s2",
  workflow_id: "wf.demo",
  run_title: "",
  state: "waiting",
  active: [],
  done: ["intake"],
  failed: [],
  waiting_at: "gate-9",
  gate: GATE,
  started_at: "2026-08-21T10:00:00Z",
};

const REPLY_PAYLOAD = {
  message: "Drafted a flow for you.",
  draft: { spec_yaml: "", title: "Freshly drafted flow", summary: "", applicability: "" },
  issues: [],
  valid: true,
  graph: { name: "fresh", version: 1, nodes: [], edges: [] },
};

const ORCHESTRATOR_MODEL = {
  id: "anthropic/claude-sonnet-4.6",
  name: "claude-sonnet-4.6",
  route: "gateway" as const,
  context_length: 200_000,
  price_in_per_m: "3",
  price_out_per_m: "15",
  reasoning: "effort" as const,
  pinned: true,
};

const TIERS: TiersListing = {
  tiers: [
    {
      tier: "orchestrator",
      role: "design judgment, multi-file changes, hard debugging, the final review",
      model: ORCHESTRATOR_MODEL,
      thinking: "high",
      default: true,
    },
    {
      tier: "worker",
      role: "ordinary implementation, review with a clear brief, debugging with a repro",
      model: ORCHESTRATOR_MODEL,
      thinking: "medium",
      default: true,
    },
    {
      tier: "helper",
      role: "mechanical edits, lookups, summaries, running a check, single-file work",
      model: ORCHESTRATOR_MODEL,
      thinking: "low",
      default: true,
    },
  ],
};

const PLUGINS: PluginsListing = {
  plugins: [
    {
      name: "run_workflow",
      description: "It can run a checked workflow you already own.",
      enabled: true,
      fixed: true,
    },
  ],
};

const SESSIONS_BY_ID: Record<string, SessionSummary> = {
  s1: STORED_SESSION,
  s2: ANSWERING_SESSION,
  s3: TOOL_SESSION,
  "s-new": NEW_SESSION,
};

type StreamChunk = Record<string, unknown>;

function sseBody(chunks: StreamChunk[]): string {
  const lines = chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("");
  return `${lines}data: [DONE]\n\n`;
}

function streamResponse(chunks: StreamChunk[] = [{ type: "start" }, { type: "finish" }]) {
  return new HttpResponse(sseBody(chunks), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function mountApi(
  overrides: {
    sessions?: SessionSummary[];
    runs?: RunView[];
    conversation?: Record<string, unknown>;
    tiers?: TiersListing;
    plugins?: PluginsListing;
  } = {},
) {
  server.use(
    http.get(`${BASE}/api/projects`, () => HttpResponse.json({ projects: PROJECTS })),
    // Every send knocks on the steer endpoint first; an idle session answers 409 and the message
    // goes out as an ordinary turn.
    http.post(`${BASE}/api/sessions/:id/steer`, () =>
      HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 }),
    ),
    http.get(`${BASE}/api/sessions/:id/conversation`, () =>
      HttpResponse.json(
        overrides.conversation ?? {
          turns: [],
          draft: { spec_yaml: "", title: "", summary: "", applicability: "" },
          issues: [],
          valid: false,
          graph: null,
        },
      ),
    ),
    http.get(`${BASE}/api/sessions`, () =>
      HttpResponse.json({ sessions: overrides.sessions ?? [STORED_SESSION, ANSWERING_SESSION] }),
    ),
    http.get(`${BASE}/api/sessions/:id`, ({ params }) => {
      const session = SESSIONS_BY_ID[String(params.id)];
      return session
        ? HttpResponse.json({ session, turns: [] })
        : HttpResponse.json({ detail: "no session with that id here" }, { status: 404 });
    }),
    http.get(`${BASE}/api/sessions/s1/transcript`, () =>
      HttpResponse.json({
        messages: [
          {
            run_id: "r1",
            idx: 0,
            kind: "request",
            payload: userPromptPayload("Hi agent"),
            text: "Hi agent",
          },
          {
            run_id: "r1",
            idx: 1,
            kind: "response",
            payload: assistantTextPayload("Hello, how can I help?"),
            text: "Hello, how can I help?",
          },
        ],
      }),
    ),
    http.get(`${BASE}/api/sessions/s2/transcript`, () =>
      HttpResponse.json({
        messages: [
          {
            run_id: "r2",
            idx: 0,
            kind: "request",
            payload: userPromptPayload("Build me a pipeline"),
            text: "Build me a pipeline",
          },
        ],
      }),
    ),
    http.get(`${BASE}/api/sessions/s-new/transcript`, () => HttpResponse.json({ messages: [] })),
    http.get(`${BASE}/api/sessions/s3/transcript`, () =>
      HttpResponse.json({
        messages: [
          {
            run_id: "r3",
            idx: 0,
            kind: "request",
            payload: userPromptPayload("load the docs skill"),
            text: "load the docs skill",
          },
          {
            run_id: "r3",
            idx: 1,
            kind: "response",
            payload: {
              kind: "response",
              parts: [{ part_kind: "tool-call", tool_name: "load_skill", args: { name: "docs" } }],
            },
            text: "",
          },
          {
            run_id: "r3",
            idx: 2,
            kind: "request",
            payload: {
              kind: "request",
              parts: [
                { part_kind: "tool-return", tool_name: "load_skill", content: HUGE_TOOL_RETURN },
              ],
            },
            text: "",
          },
          {
            run_id: "r3",
            idx: 3,
            kind: "response",
            payload: assistantTextPayload("Loaded the docs skill."),
            text: "Loaded the docs skill.",
          },
        ],
      }),
    ),
    http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: overrides.runs ?? [] })),
    http.get(`${BASE}/api/sessions/:id/run/state`, () =>
      HttpResponse.json({
        running: true,
        workflow: "wf.demo",
        result: null,
        active: [],
        done: ["intake"],
        failed: [],
        waiting_at: WAITING_RUN.waiting_at,
        events: [],
        seq: 1,
        auto_approve: false,
        gate_mode: "wait",
      }),
    ),
    http.get(`${BASE}/api/workflow/:id`, ({ params }) =>
      HttpResponse.json({
        ok: true,
        id: String(params.id),
        name: String(params.id),
        version: 1,
        description: "",
        title: "",
        summary: "",
        applicability: "",
        provisional: false,
        graph: { name: String(params.id), version: 1, nodes: [], edges: [] },
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
      }),
    ),
    http.get(`${BASE}/api/harness/tiers`, () => HttpResponse.json(overrides.tiers ?? TIERS)),
    http.get(`${BASE}/api/harness/plugins`, () => HttpResponse.json(overrides.plugins ?? PLUGINS)),
  );
}

function renderPage() {
  return render(
    <DraftProvider>
      <ChatPage />
    </DraftProvider>,
  );
}

// The left rail, scoped: a session's title can also appear elsewhere (the right rail's own live
// card), so a click meant for the session ROW names this container rather than the bare text.
function sessionsRail(): HTMLElement {
  return screen.getByText("Sessions").closest("div")?.parentElement as HTMLElement;
}

describe("ChatPage", () => {
  it("renders Sessions left, the composer center, and the status row underneath, no rail to open", async () => {
    mountApi();
    renderPage();

    expect(screen.getByText("Sessions")).toBeInTheDocument();
    expect(await screen.findByRole("radio", { name: "Wait for me" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "1 plugin" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open Controls" })).not.toBeInTheDocument();
    await screen.findByText("Stored chat");
  });

  it("with no ?session=, shows the no-session sentence and a composer for a first message, mounting no engine", async () => {
    mountApi();
    renderPage();

    await screen.findByText("Stored chat");
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
    expect(
      screen.getByText("Pick a session on the left, or write the first message of a new one."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Loading the conversation.")).not.toBeInTheDocument();
  });

  it("spends no chrome on a page header, and still names the page for a screen reader", async () => {
    mountApi();
    renderPage();

    await userEvent.click(await screen.findByText("Stored chat"));
    await screen.findByText("Hello, how can I help?");

    // The one heading is the station's own name, carried for assistive technology and drawn
    // nowhere: nothing on screen reads as a page header, and no other heading competes with it.
    const heading = screen.getByRole("heading", { level: 1, name: "Chat" });
    expect(heading).toHaveClass("sr-only");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("says the orchestrator model quietly in the composer, display only", async () => {
    mountApi();
    renderPage();

    expect(await screen.findByText("claude-sonnet-4.6")).toBeInTheDocument();
    // No picker here, chat only ever reads: no combobox, no select, no reset button.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("clicking a session row loads that transcript into the center without a POST", async () => {
    let streamCalls = 0;
    mountApi();
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, () => {
        streamCalls += 1;
        return streamResponse();
      }),
    );
    renderPage();

    await userEvent.click(await screen.findByText("Stored chat"));

    expect(await screen.findByText("Hello, how can I help?")).toBeInTheDocument();
    expect(streamCalls).toBe(0);
  });

  it("New chat creates nothing, it only opens the composer for a first message", async () => {
    mountApi();
    let posted = 0;
    server.use(
      http.post(`${BASE}/api/sessions`, () => {
        posted += 1;
        return HttpResponse.json({ session: NEW_SESSION });
      }),
    );
    renderPage();
    await userEvent.click(await screen.findByText("Stored chat"));
    await screen.findByText("Hello, how can I help?");

    await userEvent.click(within(sessionsRail()).getByRole("button", { name: "New chat" }));

    expect(posted).toBe(0);
    expect(routerReplace).toHaveBeenLastCalledWith("/chat");
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
  });

  it("the first message creates the session, navigates to it, and streams that message there", async () => {
    mountApi();
    let posted: unknown = null;
    let streamed = "";
    server.use(
      http.post(`${BASE}/api/sessions`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ session: NEW_SESSION });
      }),
      http.post(`${BASE}/api/sessions/s-new/messages`, async ({ request }) => {
        const body = await request.json();
        streamed = JSON.stringify(body);
        return streamResponse([
          { type: "start" },
          { type: "text-start", id: "t1" },
          { type: "text-delta", id: "t1", delta: "Hello back." },
          { type: "text-end", id: "t1" },
          { type: "finish" },
        ]);
      }),
    );
    renderPage();
    await screen.findByText("Stored chat");

    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Draft me a flow");
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(posted).toEqual({}));
    expect(routerReplace).toHaveBeenLastCalledWith("/chat?session=s-new");
    await waitFor(() => expect(streamed).toContain("Draft me a flow"), { timeout: 4000 });
  });

  it("does not resend the first message after switching away from the new session and back", async () => {
    mountApi({ sessions: [STORED_SESSION, ANSWERING_SESSION, NEW_SESSION] });
    let posted: unknown = null;
    let streamCalls = 0;
    server.use(
      http.post(`${BASE}/api/sessions`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ session: NEW_SESSION });
      }),
      http.post(`${BASE}/api/sessions/s-new/messages`, async ({ request }) => {
        streamCalls += 1;
        await request.json();
        return streamResponse([
          { type: "start" },
          { type: "text-start", id: "t1" },
          { type: "text-delta", id: "t1", delta: "Hello back." },
          { type: "text-end", id: "t1" },
          { type: "finish" },
        ]);
      }),
    );
    renderPage();
    await screen.findByText("Stored chat");

    await userEvent.type(
      screen.getByRole("textbox", { name: "Message" }),
      "Draft me a flow{Enter}",
    );

    await waitFor(() => expect(posted).toEqual({}));
    await waitFor(() => expect(streamCalls).toBe(1));

    // A session switch and back remounts SessionChat (`key={viewedId}`), the same shape a page
    // leave and return produces. The first message must not go out a second time.
    await userEvent.click(within(sessionsRail()).getByText("Stored chat"));
    await screen.findByText("Hello, how can I help?");
    await userEvent.click(within(sessionsRail()).getByText("session s-new"));
    await screen.findByRole("textbox", { name: "Message" });

    expect(streamCalls).toBe(1);
  });

  it("sending a message streams straight to the session's own stream, no continue step", async () => {
    mountApi();
    renderPage();

    await userEvent.click(await screen.findByText("Stored chat"));
    await screen.findByText("Hello, how can I help?");

    let hitPath = "";
    server.use(
      http.post(`${BASE}/api/sessions/s1/messages`, ({ request }) => {
        hitPath = new URL(request.url).pathname;
        return streamResponse([
          { type: "start" },
          { type: "text-start", id: "t1" },
          { type: "text-delta", id: "t1", delta: "Drafted a flow for you." },
          { type: "text-end", id: "t1" },
          { type: "data-settle", data: REPLY_PAYLOAD },
          { type: "finish" },
        ]);
      }),
    );

    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "carry on{Enter}");

    expect(await screen.findByText("Freshly drafted flow")).toBeInTheDocument();
    expect(hitPath).toBe("/api/sessions/s1/messages");
  });

  it("keeps the composer open while the viewed session is answering, beside the Stop button", async () => {
    // The send steers the running turn through the steer endpoint; answering never locks the box.
    mountApi();
    renderPage();

    await within(sessionsRail()).findByText("Answering chat");
    await userEvent.click(within(sessionsRail()).getByText("Answering chat"));
    await screen.findByText("Build me a pipeline");

    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("Stop calls interrupt for the viewed session", async () => {
    mountApi();
    let interruptCalls = 0;
    server.use(
      http.post(`${BASE}/api/sessions/s2/interrupt`, () => {
        interruptCalls += 1;
        return HttpResponse.json({ ok: true });
      }),
    );
    renderPage();

    await within(sessionsRail()).findByText("Answering chat");
    await userEvent.click(within(sessionsRail()).getByText("Answering chat"));
    await screen.findByText("Build me a pipeline");

    await userEvent.click(screen.getByRole("button", { name: "Stop" }));

    expect(interruptCalls).toBe(1);
  });

  it("the gate turn appears from a waiting run naming the viewed session, composer stays enabled", async () => {
    mountApi({ runs: [WAITING_RUN] });
    renderPage();

    await userEvent.click(await screen.findByText("Stored chat"));
    await screen.findByText("Hello, how can I help?");
    // A run can be waiting for a session other than the one on screen; the gate turn is scoped
    // to the viewed session, unlike the right rail's own live card.
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();

    await userEvent.click(within(sessionsRail()).getByText("Answering chat"));
    await screen.findByText("Build me a pipeline");

    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) =>
          element?.tagName.toLowerCase() === "p" &&
          element.textContent === "gate-9 is waiting for you.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled();
  });

  it("deciding posts to the viewed session's own decide endpoint", async () => {
    mountApi({ runs: [WAITING_RUN] });
    let decidedBody: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/s2/decide`, async ({ request }) => {
        decidedBody = await request.json();
        return HttpResponse.json({ ok: true, approved: true });
      }),
    );
    renderPage();

    await within(sessionsRail()).findByText("Answering chat");
    await userEvent.click(within(sessionsRail()).getByText("Answering chat"));
    await screen.findByRole("button", { name: "Approve" });

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(decidedBody).toEqual({ approve: true });
  });

  it("clicking an assistant bubble opens the turn inspector; tool traffic never became a bubble", async () => {
    mountApi({ sessions: [STORED_SESSION, ANSWERING_SESSION, TOOL_SESSION] });
    renderPage();

    await userEvent.click(await screen.findByText("Tool chat"));
    await screen.findByText("Loaded the docs skill.");

    // The tool call and its huge return never reached the main thread as bubbles.
    expect(screen.queryByText(HUGE_TOOL_RETURN, { exact: false })).not.toBeInTheDocument();
    expect(screen.queryByText("load_skill")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "View turn detail" }));

    const inspector = await screen.findByRole("dialog", { name: "Turn detail" });
    expect(within(inspector).getByText("load the docs skill")).toBeInTheDocument();
    expect(within(inspector).getAllByText("load_skill")).toHaveLength(2);
    expect(
      within(inspector).queryByText(HUGE_TOOL_RETURN, { exact: false }),
    ).not.toBeInTheDocument();

    await userEvent.click(within(inspector).getByRole("button", { name: "View" }));

    const viewer = await screen.findByRole("dialog", { name: "load_skill return" });
    expect(within(viewer).getByText(/skill body line/)).toBeInTheDocument();
  });

  it("a re-opened session's draft card survives, fed by the conversation read", async () => {
    mountApi({
      conversation: {
        turns: [],
        draft: {
          spec_yaml: "name: gate-check\n",
          title: "gate-check",
          summary: "",
          applicability: "",
        },
        issues: [],
        valid: true,
        graph: { name: "gate-check", version: 1, nodes: [], edges: [] },
      },
    });
    renderPage();

    await userEvent.click(await screen.findByText("Stored chat"));
    await screen.findByText("Hello, how can I help?");

    expect(await screen.findByText("gate-check")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start this run" })).toBeInTheDocument();
  });

  it("closes the inspector when the viewed session changes", async () => {
    mountApi({ sessions: [STORED_SESSION, ANSWERING_SESSION, TOOL_SESSION] });
    renderPage();

    await userEvent.click(await screen.findByText("Tool chat"));
    await screen.findByText("Loaded the docs skill.");
    await userEvent.click(screen.getByRole("button", { name: "View turn detail" }));
    await screen.findByRole("dialog", { name: "Turn detail" });

    await userEvent.click(within(sessionsRail()).getByText("Stored chat"));
    await screen.findByText("Hello, how can I help?");

    expect(screen.queryByRole("dialog", { name: "Turn detail" })).not.toBeInTheDocument();
  });

  it("Change the gates, from the draft card, focuses the statusline's first gate radio", async () => {
    mountApi({
      conversation: {
        turns: [],
        draft: { spec_yaml: "name: x\n", title: "x", summary: "", applicability: "" },
        issues: [],
        valid: true,
        graph: { name: "x", version: 1, nodes: [], edges: [] },
      },
    });
    renderPage();

    await userEvent.click(await screen.findByText("Stored chat"));
    await screen.findByRole("button", { name: "Change the gates" });

    await userEvent.click(screen.getByRole("button", { name: "Change the gates" }));

    expect(await screen.findByRole("radio", { name: "Wait for me" })).toHaveFocus();
  });

  it("says nothing about a run and shows no run actions while nothing is live", async () => {
    mountApi();
    renderPage();

    await userEvent.click(await screen.findByText("Stored chat"));

    await screen.findByRole("link", { name: "1 plugin" });
    expect(screen.queryByText("No run in this session.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("shows the live run's actions in the status row when the viewed session's run is live", async () => {
    mountApi({ runs: [WAITING_RUN] });
    renderPage();

    await userEvent.click(await screen.findByText("Stored chat"));
    await screen.findByRole("link", { name: "1 plugin" });
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();

    await userEvent.click(within(sessionsRail()).getByText("Answering chat"));

    expect(await screen.findByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });
});
