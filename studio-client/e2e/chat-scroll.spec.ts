import { type Page, type Route, expect, test } from "@playwright/test";

// The calm-scroll acceptance criterion end to end: the conversation sticks to the bottom only
// while the reader is there; scrolled up, a new streamed turn leaves it in place and a control
// jumps back down. Same convention as e2e/follow-me.spec.ts: every endpoint the visited page
// fetches is page.route-mocked, including the chat stream itself, since the studio backend's
// default origin is a real server this suite must never reach.

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

type StreamChunk = Record<string, unknown>;

function sseBody(chunks: StreamChunk[]): string {
  const lines = chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join("");
  return `${lines}data: [DONE]\n\n`;
}

function stream(route: Route, chunks: StreamChunk[]) {
  return route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: sseBody(chunks),
  });
}

const GRAPH = { nodes: [], edges: [] };

const KNOWLEDGE_BASES = {
  knowledge_bases: [
    {
      name: "research",
      registered: true,
      indexed: true,
      broken: false,
      path: "/knowledge-bases/research",
      description: "",
      pages: 12,
      page_types: ["paper"],
      last_indexed: "2026-08-11T09:00:00+00:00",
    },
  ],
};

const KNOWLEDGE_BASE_DETAIL = {
  ...KNOWLEDGE_BASES.knowledge_bases[0],
  sources_provider: null,
  sources_providers: [],
  feeds: [],
  validation: [],
  intake: [],
  last_run: null,
  schedule: null,
};

function usage() {
  return { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 };
}

const TRANSCRIPT_TURNS = 25;

function userPromptPayload(text: string) {
  return { kind: "request", parts: [{ part_kind: "user-prompt", content: text }] };
}

function textResponsePayload(text: string) {
  return { kind: "response", parts: [{ part_kind: "text", content: text }] };
}

// Enough words per turn, over enough turns, that the transcript overflows the viewport well
// before the reader reaches the end of it.
function assistantText(turn: number): string {
  return Array.from(
    { length: 5 },
    (_, line) =>
      `Turn ${turn} line ${line + 1}: filling the transcript so the conversation can be scrolled.`,
  ).join(" ");
}

const TRANSCRIPT: Array<{
  run_id: string;
  idx: number;
  kind: string;
  payload: unknown;
  text: string;
}> = [];
for (let turn = 0; turn < TRANSCRIPT_TURNS; turn += 1) {
  const question = `Question number ${turn}.`;
  const answer = assistantText(turn);
  TRANSCRIPT.push({
    run_id: `r${turn}`,
    idx: 0,
    kind: "request",
    payload: userPromptPayload(question),
    text: question,
  });
  TRANSCRIPT.push({
    run_id: `r${turn}`,
    idx: 1,
    kind: "response",
    payload: textResponsePayload(answer),
    text: answer,
  });
}

const SESSION_S1 = {
  id: "s1",
  project_id: "proj-1",
  title: "Long transcript",
  started_at: "2026-08-23T10:00:00Z",
  last_activity_at: "2026-08-23T10:00:00Z",
  archived: false,
  answering: false,
  turn_count: TRANSCRIPT_TURNS,
  usage: usage(),
  kind: "chat",
  cost_usd: "0",
  context: null,
};

const MASTER_SESSION = { ...SESSION_S1, id: "m-1", kind: "master" };

const CONVERSATION = {
  turns: [],
  draft: { spec_yaml: "", title: "", summary: "", applicability: "", lineage: null },
  issues: [],
  valid: false,
  graph: null,
};

const NEW_TURN_TEXT = "Here is a fresh reply while you were reading further up.";

const NEW_TURN_STREAM: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "text-start", id: "t1" },
  { type: "text-delta", id: "t1", delta: NEW_TURN_TEXT },
  { type: "text-end", id: "t1" },
  { type: "finish-step" },
  { type: "finish" },
];

async function mockApi(page: Page) {
  // The dashboard opens one EventSource per backend on mount. Answered with a stream that says
  // nothing and asks for a long retry, so no spec spends its budget on reconnects.
  await page.route("**/api/events**", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "retry: 100000\n\n" }),
  );
  await page.route("**/api/graph**", (route) => json(route, GRAPH));
  await page.route("**/api/knowledge-bases", (route) => json(route, KNOWLEDGE_BASES));
  await page.route("**/api/knowledge-bases/research", (route) => json(route, KNOWLEDGE_BASE_DETAIL));
  await page.route("**/api/knowledge-bases/research/runs", (route) => json(route, { runs: [] }));
  await page.route("**/api/library", (route) => json(route, []));
  await page.route("**/api/steps", (route) => json(route, []));
  await page.route("**/api/conversation", (route) =>
    json(route, { turns: [], session_id: "s1" }),
  );
  // The follow-me rail's own master-session resolver, mounted in the shell on every route.
  await page.route("**/api/master", (route) => json(route, { session_id: "m-1" }));
  // One route covers every /api/sessions shape the chat page's own mount can fire: the list,
  // the viewed session's own transcript, detail and conversation reads, and the follow-me
  // rail's master-session detail.
  await page.route("**/api/sessions**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/sessions/s1/transcript") {
      return json(route, { messages: TRANSCRIPT });
    }
    if (url.pathname === "/api/sessions/s1/conversation") {
      return json(route, CONVERSATION);
    }
    if (url.pathname === "/api/sessions/s1") {
      return json(route, { session: SESSION_S1, turns: [] });
    }
    if (url.pathname === "/api/sessions/m-1") {
      return json(route, { session: MASTER_SESSION, turns: [] });
    }
    if (url.pathname.endsWith("/transcript")) {
      return json(route, { messages: [] });
    }
    return json(route, { sessions: [SESSION_S1] });
  });
  await page.route("**/api/runs**", (route) => json(route, { runs: [] }));
  await page.route("**/api/projects", (route) => json(route, { projects: [] }));
  await page.route("**/api/run/state**", (route) =>
    json(route, {
      auto_approve: false,
      gate_mode: "wait",
      running: false,
      workflow: null,
      result: null,
      active: [],
      done: [],
      failed: [],
      waiting_at: null,
      events: [],
      seq: 0,
    }),
  );
  await page.route("**/api/pending", (route) => json(route, { pending: null }));
  await page.route("**/api/harness/*", (route) => {
    const kind = new URL(route.request().url()).pathname.split("/").at(-1);
    return json(route, { kind, artifacts: [] });
  });
  await page.route("**/api/harness/composition", (route) =>
    json(route, {
      model: "gpt-5.4",
      run_engine: "in_process",
      steps_registered: [],
      promoted: [],
      active: { capability: 0, step: 0, agent: 0, profile: 0 },
    }),
  );
  await page.route("**/api/harness/model", (route) =>
    json(route, { model: "gpt-5.4", choices: ["gpt-5.4"], selected: null }),
  );
  await page.route("**/api/harness/selection", (route) => json(route, { name: null }));
  await page.route("**/api/harness/plugins", (route) =>
    json(route, {
      plugins: [
        {
          name: "research",
          description: "The knowledge bases' read plugin.",
          enabled: true,
          fixed: false,
        },
        { name: "sql", description: "The SQL plugin.", enabled: false, fixed: false },
        { name: "spend-limits", description: "The money brake.", enabled: true, fixed: true },
      ],
    }),
  );
  // Every send knocks on the steer endpoint first, and an idle session answers 409 before the
  // message goes out as an ordinary turn.
  await page.route("**/api/sessions/*/steer", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "nothing is in flight in this session" }),
    }),
  );
  // The viewed session's own chat stream: every new turn answers with fresh text, appended
  // after the long transcript the reader has scrolled away from.
  await page.route("**/api/sessions/s1/messages", (route) => stream(route, NEW_TURN_STREAM));
}

test("scrolling up keeps a new turn from jumping the reader back down, and the button returns them", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/chat?session=s1");

  await expect(page.getByRole("heading", { level: 1, name: "Chat" })).toBeVisible();
  await expect(page.getByText("Question number 0.")).toBeVisible();

  const container = page.locator('[role="log"] .overflow-y-auto');
  function distanceFromBottom(): Promise<number> {
    return container.evaluate(
      (element) => element.scrollHeight - element.clientHeight - element.scrollTop,
    );
  }

  // The transcript's own mount-time stick-to-bottom animation lands here before this: scrolling
  // away while its spring is still under way would race it back to the bottom. `isAtBottom`
  // reads true from the moment the conversation mounts, before the animation reaches the
  // bottom, so the actual scroll position -- not the scroll button's visibility -- is what
  // this waits on.
  await expect.poll(distanceFromBottom).toBeLessThan(5);

  await container.evaluate((element) => {
    element.scrollTop = 0;
  });

  // Scrolled away from the bottom: the jump-back control appears, and the reader is at the top.
  await expect(page.getByRole("button", { name: "Scroll to bottom" })).toBeVisible();
  await expect.poll(() => container.evaluate((element) => element.scrollTop)).toBeLessThan(50);

  await page.getByRole("textbox", { name: "Message" }).fill("keep me where I am");
  await page.keyboard.press("Enter");

  await expect(page.getByText(NEW_TURN_TEXT)).toBeVisible();

  // The new turn landed off-screen, well past the top; the reader was not dragged down to it.
  const scrollTopAfterSend = await container.evaluate((element) => element.scrollTop);
  expect(scrollTopAfterSend).toBeLessThan(50);
  await expect(page.getByRole("button", { name: "Scroll to bottom" })).toBeVisible();

  await page.getByRole("button", { name: "Scroll to bottom" }).click();

  await expect(page.getByRole("button", { name: "Scroll to bottom" })).not.toBeVisible();
  await expect
    .poll(() =>
      container.evaluate(
        (element) => element.scrollTop + element.clientHeight >= element.scrollHeight - 2,
      ),
    )
    .toBe(true);
});
