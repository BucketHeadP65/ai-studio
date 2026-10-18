import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DraftProvider } from "@/api/draft-store";
import type { ChatReply, RunHistoryRecord } from "@/api/studio-types";
import { FollowMeProvider } from "@/components/follow-me/provider";
import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import DraftWorkflowPage from "./page";

// The draft inspector reads either a named session's own draft (`?session=`)
// or the in-memory store the authoring conversation last settled, when no
// session is named. With `?run=` instead it reads a run's own archived
// record -- a run launched from a draft never saved, so its workflow id is
// "draft" and its saved-workflow lookup 404s forever; that lookup's own
// tests live where it is exercised generically, on the `[id]` route (the
// same `WorkflowView` this page renders under workflow id "draft"). Every
// request the page's own mount can fire is mocked here (MSW errors on
// anything unhandled).

let searchParamsValue = new URLSearchParams();
const routerPush = vi.fn();
const routerBack = vi.fn();
const routerReplace = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => "/workflows/draft",
  useRouter: () => ({ push: routerPush, back: routerBack, replace: routerReplace }),
  useSearchParams: () => searchParamsValue,
}));

afterEach(() => {
  searchParamsValue = new URLSearchParams();
  routerPush.mockClear();
  routerBack.mockClear();
  routerReplace.mockClear();
});

const BASE = "http://studio.test";

const DESCRIBED = { executable: true, described: true, findings: [] };

const REPLY: ChatReply = {
  message: "Drafted lit-check.",
  draft: { spec_yaml: "name: lit-check\n", title: "lit-check", summary: "", applicability: "" },
  issues: [],
  valid: true,
  intent_issues: [],
  readiness: DESCRIBED,
  graph: {
    name: "lit-check",
    version: 1,
    nodes: [
      {
        key: "a",
        id: "a",
        kind: "run",
        signature: "",
        detail: "",
        tools: [],
        required: true,
        is_output: false,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
      },
    ],
    edges: [],
  },
};

function renderPage() {
  return render(
    <DraftProvider>
      <FollowMeProvider>
        <DraftWorkflowPage />
      </FollowMeProvider>
    </DraftProvider>,
  );
}

describe("DraftWorkflowPage", () => {
  it("with no ?session=, falls back to no draft in memory", () => {
    renderPage();

    expect(screen.getByText("No draft in memory.")).toBeInTheDocument();
  });

  it("with ?session=, renders that session's fetched draft: title, validity, graph", async () => {
    searchParamsValue = new URLSearchParams("session=s1");
    server.use(
      http.get(`${BASE}/api/sessions/s1/conversation`, () =>
        HttpResponse.json({
          turns: [{ who: "you", text: "draft me a check" }],
          draft: {
            spec_yaml: "name: lit-check\n",
            title: "lit-check",
            summary: "",
            applicability: "",
          },
          issues: [],
          valid: true,
          readiness: DESCRIBED,
          graph: REPLY.graph,
        }),
      ),
    );
    renderPage();

    expect(await screen.findByRole("heading", { level: 2, name: "lit-check" })).toBeInTheDocument();
    expect(screen.getByText("Valid, this app can run it.")).toBeInTheDocument();
  });

  it("says an adapted draft is a new version of its parent, and what it was adapted from", async () => {
    searchParamsValue = new URLSearchParams("session=s1");
    server.use(
      http.get(`${BASE}/api/sessions/s1/conversation`, () =>
        HttpResponse.json({
          turns: [],
          draft: {
            spec_yaml: "name: lit-check\n",
            title: "lit-check",
            summary: "",
            applicability: "",
            lineage: {
              parent_name: "lit-check",
              parent_version: 5,
              touched: [],
              ops: [{ op: "set_description", to: "Say when the reading is too thin." }],
              evidence:
                "Adapted from run run-4 of wf.lit-check.v5.\nObserved outcome: partial. One section came back empty.",
            },
          },
          issues: [],
          valid: true,
          readiness: DESCRIBED,
          graph: REPLY.graph,
        }),
      ),
    );
    renderPage();

    const note = await waitFor(() => {
      const found = document.querySelector('[data-section="adapted-from"]');
      expect(found).not.toBeNull();
      return found as HTMLElement;
    });
    expect(note).toHaveTextContent("A new version of lit-check v5");
    expect(note).toHaveTextContent("The one it came from is unchanged.");
    expect(note).toHaveTextContent("Adapted from run run-4 of wf.lit-check.v5.");
    expect(note).toHaveTextContent("Observed outcome: partial.");
  });

  it("shows the session's issues and disables Save when the draft is not valid", async () => {
    searchParamsValue = new URLSearchParams("session=s1");
    server.use(
      http.get(`${BASE}/api/sessions/s1/conversation`, () =>
        HttpResponse.json({
          turns: [],
          draft: { spec_yaml: "", title: "half a draft", summary: "", applicability: "" },
          issues: ["[wiring] draft: unknown input"],
          valid: false,
          graph: REPLY.graph,
        }),
      ),
    );
    renderPage();

    expect(await screen.findByText(/unknown input/)).toBeInTheDocument();
    expect(screen.getByText("Not valid yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("saves a valid session draft and confirms with a link to the library", async () => {
    searchParamsValue = new URLSearchParams("session=s1");
    server.use(
      http.get(`${BASE}/api/sessions/s1/conversation`, () =>
        HttpResponse.json({
          turns: [],
          draft: {
            spec_yaml: "name: lit-check\n",
            title: "lit-check",
            summary: "",
            applicability: "",
          },
          issues: [],
          valid: true,
          readiness: DESCRIBED,
          graph: REPLY.graph,
        }),
      ),
      http.post(`${BASE}/api/sessions/s1/save`, () =>
        HttpResponse.json({ ok: true, id: "wf.lit-check.v1" }),
      ),
    );
    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Save" }));

    expect(await screen.findByText(/Saved as wf.lit-check.v1/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open it?" })).toHaveAttribute(
      "href",
      "/workflows/wf.lit-check.v1",
    );
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("with ?session= but no draft yet, asks only what the automation should do", async () => {
    searchParamsValue = new URLSearchParams("session=s1");
    server.use(
      http.get(`${BASE}/api/sessions/s1/conversation`, () =>
        HttpResponse.json({
          turns: [],
          draft: { spec_yaml: "", title: "", summary: "", applicability: "" },
          issues: [],
          valid: false,
          graph: null,
        }),
      ),
    );
    renderPage();

    expect(
      await screen.findByRole("heading", { name: "What should this automation do?" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
  });

  it("surfaces a 404 for an unknown session", async () => {
    searchParamsValue = new URLSearchParams("session=gone");
    server.use(
      http.get(`${BASE}/api/sessions/gone/conversation`, () =>
        HttpResponse.json({ detail: "no session with that id here" }, { status: 404 }),
      ),
    );
    renderPage();

    expect(await screen.findByText(/Could not open this session's draft/)).toBeInTheDocument();
  });

  it("with ?run=, renders the run inspector for a run launched from a draft, not the empty draft page", async () => {
    const RUN_RECORD: RunHistoryRecord = {
      run_id: "run-99",
      workflow_id: "draft",
      run_title: "",
      outcome: "completed",
      failure: "",
      started_at: "2026-08-30T10:00:00Z",
      ended_at: "2026-08-30T10:02:00Z",
      runner: "app:chat",
      session_id: "sess-draft",
      params: {},
      gates_approved: [],
      gates_rejected: {},
      gates_auto: [],
      gates_orchestrated: [],
      pinned: false,
      spec: {},
      values: { a: "draft run output" },
      conversations: {},
      owner_reviews: [],
      events: [{ seq: 1, node: "a", at: 1, finished: true, failed: false }],
      graph: {
        name: "a run from a draft",
        version: 0,
        nodes: [
          {
            key: "a",
            id: "a",
            kind: "run",
            signature: "() -> str",
            detail: "",
            tools: [],
            required: true,
            is_output: true,
            unknown: false,
            brought: false,
            briefing: "",
            input_schemas: {},
            output_schema: null,
            source: "",
          },
        ],
        edges: [],
      },
    };
    searchParamsValue = new URLSearchParams("run=run-99");
    server.use(
      http.get(`${BASE}/api/workflow/draft`, () =>
        HttpResponse.json({ detail: "no such workflow" }, { status: 404 }),
      ),
      http.get(`${BASE}/api/runs`, () => HttpResponse.json({ runs: [] })),
      http.get(`${BASE}/api/workflows/draft/history`, () =>
        HttpResponse.json({ runs: [RUN_RECORD] }),
      ),
      http.get(`${BASE}/api/runs/history/run-99`, () => HttpResponse.json(RUN_RECORD)),
    );

    renderPage();

    expect(screen.queryByText("No draft in memory.")).not.toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "a run from a draft v0" }),
    ).toBeInTheDocument();
  });
});

describe("the draft page's own save gate", () => {
  it("will not save a valid draft nobody described, and says which words are missing", async () => {
    searchParamsValue = new URLSearchParams("session=s1");
    server.use(
      http.get(`${BASE}/api/sessions/s1/conversation`, () =>
        HttpResponse.json({
          turns: [],
          draft: {
            spec_yaml: "name: lit-check\n",
            title: "lit-check",
            summary: "",
            applicability: "",
          },
          issues: [],
          valid: true,
          readiness: {
            executable: true,
            described: false,
            findings: [
              {
                field: "summary",
                severity: "error",
                message: "A summary is the one line a reader chooses by. Write one.",
              },
            ],
          },
          graph: REPLY.graph,
        }),
      ),
    );
    renderPage();

    expect(
      await screen.findByText("A summary is the one line a reader chooses by. Write one."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});

// The guided path, end to end over the mocked API: one request goes out as one chat turn, the
// draft that comes back carries what the agent recorded, the person edits an assumption, and a
// reload of the session reads their edit back. The two handlers that would make something happen
// are registered and asserted never called, because designing is designing.

const INTENT = {
  request: "Every Monday, tell me which of my open questions the week's reading answered",
  expected_result: "A short list of questions with the reading that answers each one.",
  success_example: "",
  empty_input_behavior: "",
  failure_behavior: "",
  trigger: "scheduled",
  assumptions: ["The knowledge base to read is the one this session already names"],
};

const DRAFTED = {
  spec_yaml: "name: monday-sweep\nversion: 1\n",
  title: "Monday question sweep",
  summary: "Says which open questions the week's reading answered.",
  applicability: "Use at the start of a week.",
  intent: INTENT,
};

const STILL_OPEN = ["success_example", "empty_input_behavior", "failure_behavior"];

// The chat stream in the protocol the engine reads: one `data:` line per chunk, closed by
// `data: [DONE]`. The settled draft rides the last one, as the server sends it.
function streamResponse(chunks: Record<string, unknown>[]) {
  const lines = chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("");
  return new HttpResponse(`${lines}data: [DONE]\n\n`, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("designing an automation from one request", () => {
  it("takes the request, shows what was recorded, keeps an edited assumption across a reload, and starts nothing", async () => {
    searchParamsValue = new URLSearchParams("session=s1");
    const started = vi.fn();
    const scheduled = vi.fn();
    let held = { ...DRAFTED };
    let drafted = false;
    const sent: unknown[] = [];

    server.use(
      http.get(`${BASE}/api/sessions/s1/conversation`, () =>
        HttpResponse.json(
          drafted
            ? {
                turns: [],
                draft: held,
                issues: [],
                valid: true,
                readiness: DESCRIBED,
                graph: REPLY.graph,
                intent_issues: STILL_OPEN,
              }
            : {
                turns: [],
                draft: { spec_yaml: "", title: "", summary: "", applicability: "" },
                issues: [],
                valid: false,
                graph: null,
              },
        ),
      ),
      // Every send knocks on the steer endpoint first. An idle session answers 409 and the message
      // goes out as an ordinary turn.
      http.post(`${BASE}/api/sessions/s1/steer`, () =>
        HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 }),
      ),
      http.post(`${BASE}/api/sessions/s1/messages`, async ({ request }) => {
        sent.push(await request.json());
        drafted = true;
        return streamResponse([
          { type: "start" },
          { type: "start-step" },
          { type: "text-start", id: "t-1" },
          { type: "text-delta", id: "t-1", delta: "Here is the shape it would run." },
          { type: "text-end", id: "t-1" },
          {
            type: "data-settle",
            data: {
              message: "Here is the shape it would run.",
              draft: held,
              issues: [],
              valid: true,
              readiness: DESCRIBED,
              graph: REPLY.graph,
              intent_issues: STILL_OPEN,
            },
          },
          { type: "finish-step" },
          { type: "finish" },
        ]);
      }),
      http.put(`${BASE}/api/sessions/s1/intent`, async ({ request }) => {
        const body = (await request.json()) as typeof INTENT;
        held = { ...held, intent: body };
        return HttpResponse.json({
          ok: true,
          draft: held,
          issues: [],
          valid: true,
          readiness: DESCRIBED,
          graph: REPLY.graph,
          intent_issues: STILL_OPEN,
        });
      }),
      http.post(`${BASE}/api/sessions/s1/run`, () => {
        started();
        return HttpResponse.json({ ok: true, run_id: "never" });
      }),
      http.post(`${BASE}/api/schedules`, () => {
        scheduled();
        return HttpResponse.json({ ok: true });
      }),
    );

    const { unmount } = renderPage();

    await userEvent.type(
      await screen.findByRole("textbox"),
      "Every Monday, tell me which of my open questions the week's reading answered",
    );
    await userEvent.click(screen.getByRole("button", { name: "Design it" }));

    expect(await screen.findByText(INTENT.request)).toBeInTheDocument();
    expect(sent).toHaveLength(1);
    expect((sent[0] as { design_only?: unknown }).design_only).toBe(true);
    expect(
      screen.getByText(
        "3 answers are still open. It is fine to leave them, and the draft can still be saved.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Inferred")).toBeVisible();
    expect(screen.getByRole("button", { name: "Details" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );

    await userEvent.clear(screen.getByLabelText("Assumption 1"));
    await userEvent.type(screen.getByLabelText("Assumption 1"), "I pick the knowledge base myself");
    await userEvent.click(screen.getByRole("button", { name: "Save what you asked for" }));

    await screen.findByDisplayValue("I pick the knowledge base myself");
    unmount();
    renderPage();

    expect(await screen.findByLabelText("Assumption 1")).toHaveValue(
      "I pick the knowledge base myself",
    );
    expect(started).not.toHaveBeenCalled();
    expect(scheduled).not.toHaveBeenCalled();
  });

  it("puts the YAML and the execution graph under Details, opened only when asked", async () => {
    searchParamsValue = new URLSearchParams("session=s1");
    server.use(
      http.get(`${BASE}/api/sessions/s1/conversation`, () =>
        HttpResponse.json({
          turns: [],
          draft: DRAFTED,
          issues: [],
          valid: true,
          readiness: DESCRIBED,
          graph: REPLY.graph,
          intent_issues: STILL_OPEN,
        }),
      ),
    );
    renderPage();

    const details = await screen.findByRole("button", { name: "Details" });
    expect(screen.queryByText(/name: monday-sweep/)).not.toBeInTheDocument();

    await userEvent.click(details);

    expect(details).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/name: monday-sweep/)).toBeInTheDocument();
  });
});

describe("answering the proposal without leaving the page", () => {
  it("sends a second request to the same design handler, marked design only, and still starts nothing", async () => {
    searchParamsValue = new URLSearchParams("session=s1");
    const started = vi.fn();
    const scheduled = vi.fn();
    const sent: Record<string, unknown>[] = [];
    const ANSWERED = "Split in two, then. The second step is now two steps.";
    let turns = [
      { who: "you", text: "Every Monday, sweep my open questions" },
      { who: "agent", text: "Here is the shape it would run. Read it and tell me what to change." },
    ];
    const REDRAFTED = {
      ...DRAFTED,
      spec_yaml: "name: monday-sweep\nversion: 2\n",
      title: "Monday question sweep",
    };
    let answered = false;

    server.use(
      http.get(`${BASE}/api/sessions/s1/conversation`, () =>
        HttpResponse.json({
          turns,
          draft: answered ? REDRAFTED : DRAFTED,
          issues: [],
          valid: true,
          readiness: DESCRIBED,
          graph: REPLY.graph,
          intent_issues: STILL_OPEN,
        }),
      ),
      http.post(`${BASE}/api/sessions/s1/steer`, () =>
        HttpResponse.json({ detail: "nothing is in flight in this session" }, { status: 409 }),
      ),
      http.post(`${BASE}/api/sessions/s1/messages`, async ({ request }) => {
        sent.push((await request.json()) as Record<string, unknown>);
        answered = true;
        turns = [
          ...turns,
          { who: "you", text: "Split the second step in two" },
          { who: "agent", text: ANSWERED },
        ];
        return streamResponse([
          { type: "start" },
          { type: "start-step" },
          { type: "text-start", id: "t-2" },
          { type: "text-delta", id: "t-2", delta: ANSWERED },
          { type: "text-end", id: "t-2" },
          {
            type: "data-settle",
            data: {
              message: ANSWERED,
              draft: REDRAFTED,
              issues: [],
              valid: true,
              readiness: DESCRIBED,
              graph: REPLY.graph,
              intent_issues: STILL_OPEN,
            },
          },
          { type: "finish-step" },
          { type: "finish" },
        ]);
      }),
      http.post(`${BASE}/api/sessions/s1/run`, () => {
        started();
        return HttpResponse.json({ ok: true, run_id: "never" });
      }),
      http.post(`${BASE}/api/schedules`, () => {
        scheduled();
        return HttpResponse.json({ ok: true });
      }),
    );

    renderPage();

    expect(
      await screen.findByText("Here is the shape it would run. Read it and tell me what to change."),
    ).toBeInTheDocument();
    await userEvent.type(
      screen.getByLabelText("What should change?"),
      "Split the second step in two",
    );
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.design_only).toBe(true);
    // The settled turn invalidates the conversation, so the agent's answer and the redrafted
    // proposal both land on the page the person is already reading.
    expect(await screen.findByText(ANSWERED)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(await screen.findByText(/version: 2/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open this session in the chat" })).toHaveAttribute(
      "href",
      "/chat?session=s1",
    );
    expect(started).not.toHaveBeenCalled();
    expect(scheduled).not.toHaveBeenCalled();
  });
});
