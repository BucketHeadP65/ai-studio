import { type Page, type Route, expect, test } from "@playwright/test";

// The follow-me loop end to end: the companion proposes a console act, the person approves it
// from the takeover card, and the station that owns the affected state shows the result once the
// turn settles. Same convention as e2e/smoke.spec.ts: every endpoint the visited page fetches is
// page.route-mocked, including the chat stream itself, since the studio backend's default origin
// is a real server this suite must never reach.

interface ChatStreamBody {
  tool_results?: Array<{ approved?: boolean }>;
}

type StreamChunk = Record<string, unknown>;

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

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

const NEW_SESSION = {
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

const PLUGINS_BASE = [
  {
    name: "research",
    description: "The knowledge bases' read plugin.",
    enabled: true,
    fixed: false,
  },
  { name: "sql", description: "The SQL plugin.", enabled: false, fixed: false },
  { name: "spend-limits", description: "The money brake.", enabled: true, fixed: true },
];

const SETTLE_PAYLOAD = {
  message: "Plugin sql is on.",
  draft: { spec_yaml: "", title: "", summary: "", applicability: "", lineage: null },
  issues: [],
  valid: false,
  graph: null,
};

// The companion announces the act and waits: the turn ends deferred, no data-settle chunk.
const PENDING_APPROVAL: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-input-start", toolCallId: "c-1", toolName: "toggle_plugin" },
  {
    type: "tool-input-available",
    toolCallId: "c-1",
    toolName: "toggle_plugin",
    input: { name: "sql", on: true },
  },
  { type: "tool-approval-request", approvalId: "c-1", toolCallId: "c-1" },
  { type: "finish-step" },
  { type: "finish" },
];

// The continuation once the person approves: the act's own outcome, then the turn's closing
// text, in separate steps. The adapter opens a new step before every model response after the
// first, so an act's outcome never shares a step with the text that follows it.
const SETTLED_AFTER_APPROVAL: StreamChunk[] = [
  { type: "start" },
  { type: "start-step" },
  { type: "tool-output-available", toolCallId: "c-1", output: "Plugin sql is on." },
  { type: "finish-step" },
  { type: "start-step" },
  { type: "text-start", id: "t1" },
  { type: "text-delta", id: "t1", delta: "Plugin sql is on." },
  { type: "text-end", id: "t1" },
  { type: "data-settle", data: SETTLE_PAYLOAD },
  { type: "finish-step" },
  { type: "finish" },
];

async function mockApi(page: Page) {
  // Flipped by the chat stream mock once it sees the person's approval, read back by the plugins
  // mock: the same proof point smoke.spec.ts's own knowledge-bases test uses for a mutation-then-refetch.
  let sqlOpen = false;

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
    json(route, { turns: [], session_id: "m-1" }),
  );
  // The follow-me rail's own master-session resolver, mounted in the shell on every route.
  await page.route("**/api/master", (route) => json(route, { session_id: "m-1" }));
  // One route covers every /api/sessions shape the harness page's own mount can fire: the list,
  // a just-created session's transcript, and the follow-me rail's master-session detail.
  await page.route("**/api/sessions**", (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "POST" && url.pathname === "/api/sessions") {
      return json(route, { session: NEW_SESSION });
    }
    if (url.pathname.endsWith("/transcript")) {
      return json(route, { messages: [] });
    }
    if (url.pathname === "/api/sessions/m-1") {
      return json(route, {
        session: { ...NEW_SESSION, id: "m-1", kind: "master", cost_usd: "0", unpriced: 0, context: null },
        turns: [],
      });
    }
    return json(route, { sessions: [] });
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
  // Stateful: the plugin the companion's act names here is the one the plugins card must show
  // flipped once the turn settles and the dashboard refetches.
  await page.route("**/api/harness/plugins", (route) =>
    json(route, {
      plugins: PLUGINS_BASE.map((plugin) =>
        plugin.name === "sql" ? { ...plugin, enabled: sqlOpen } : plugin,
      ),
    }),
  );
  // The chat stream: the first turn proposes the act and waits, the second (carrying the
  // person's approval) settles it. A result whose approval is not `true` never reaches this
  // mock in this spec, so only that one branch needs telling apart.
  // Every send knocks on the steer endpoint first, and an idle session answers 409 before the
  // message goes out as an ordinary turn.
  await page.route("**/api/sessions/*/steer", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "nothing is in flight in this session" }),
    }),
  );
  await page.route("**/api/sessions/m-1/messages", (route) => {
    const body = route.request().postDataJSON() as ChatStreamBody | null;
    if (body?.tool_results?.[0]?.approved === true) {
      sqlOpen = true;
      return stream(route, SETTLED_AFTER_APPROVAL);
    }
    return stream(route, PENDING_APPROVAL);
  });
}

test("the companion toggles a plugin after approval and the station shows it", async ({ page }) => {
  await mockApi(page);
  await page.goto("/harness");
  await expect(page.getByText("sql", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Companion" }).click();
  await page.getByRole("textbox", { name: "Message" }).fill("turn on the sql plugin");
  await page.keyboard.press("Enter");

  await expect(page.getByText("The companion wants to act")).toBeVisible();
  await page.getByRole("button", { name: "Approve toggle_plugin" }).click();

  // The act's outcome is an earlier step of the turn, listed above the closing text that is the
  // bubble's answer. A settled turn keeps its earlier steps open unless the person chose to fold
  // them, so the outcome shows in place without a click.
  await expect(page.getByText("Plugin sql is on.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "1 earlier step" })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await expect(page.getByText("toggle_plugin: Plugin sql is on.")).toBeVisible();
  // The companion is still up, mid-conversation, not crashed or collapsed by the round trip.
  await expect(page.getByText("Companion", { exact: true })).toBeVisible();
  // The plugins card refetched after the act's outcome and reads the flipped mock.
  await expect(page.getByRole("switch", { name: "sql" })).toHaveAttribute("aria-checked", "true");
});
