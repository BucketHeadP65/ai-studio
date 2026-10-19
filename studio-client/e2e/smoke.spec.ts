import { type Page, type Route, expect, test } from "@playwright/test";

// Smoke walk of the stations. The backends are NOT running for e2e — EVERY
// endpoint a visited page fetches on mount is page.route-mocked here. This is
// the hard lesson from client/'s rotted e2e suite: an unmocked endpoint hangs
// the page (the renderer awaits a fetch that never resolves), so the assertions
// time out far from the real cause. We mock a knowledge base's /api/graph (now read
// from its own Knowledge-bases detail page, not a station of its own) and the
// Chat station's mount-time surface (a DIFFERENT backend — the studio agent
// backend, `studio.agent`, on :8770 — so the patterns match by path alone).
// Payloads use the shapes from src/api/types.ts so each page's zod boundary
// parses them.
//
// Assertions stay ON THE SHARED UI CONTRACT ONLY (each station's <h1> + the
// nav) — station internals change without this spec's involvement.

const GRAPH = { nodes: [], edges: [] };

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

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

// The keys the detail contract asks for today. A rename here is what silently emptied this page in
// the walk below: the boundary refuses a payload that misses one, and the page shows its alert
// instead of its own heading.
const KNOWLEDGE_BASE_DETAIL = {
  ...KNOWLEDGE_BASES.knowledge_bases[0],
  sources_connectors: [],
  watch_list: [],
  validation: [],
  source_queue: [],
  last_refresh_run: null,
  schedule: null,
};

function usage() {
  return { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 };
}

// New chat's own answer: a fresh, empty session, unwrapped from `{session}`.
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
  context: null,
};

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
  await page.route("**/api/knowledge-bases/research/decisions", (route) =>
    json(route, { rows: [], skipped: [], outcomes_by_page_type: {} }),
  );
  await page.route("**/api/library", (route) => json(route, []));
  await page.route("**/api/steps", (route) => json(route, []));
  await page.route("**/api/conversation", (route) =>
    json(route, { turns: [], session_id: "s-1" }),
  );
  // The follow-me rail's own master-session resolver, mounted in the shell on every route.
  await page.route("**/api/master", (route) => json(route, { session_id: "m-1" }));
  // One route covers every /api/sessions shape the chat page's own mount can
  // fire: the list, New chat's POST, a just-created session's transcript, and
  // the follow-me rail's master-session detail.
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
        session: { ...NEW_SESSION, id: "m-1", kind: "master", cost_usd: "0", context: null },
        turns: [],
      });
    }
    return json(route, { sessions: [] });
  });
  await page.route("**/api/runs**", (route) => json(route, { runs: [] }));
  await page.route("**/api/projects", (route) => json(route, { projects: [] }));
  await page.route("**/api/run/state**", (route) =>
    json(route, {
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
}

test("the root lands on Home and the nav walks every station", async ({ page }) => {
  await mockApi(page);

  // The root IS a station now: Home, and it leads with starting an automation.
  await page.goto("/");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Create workflow" })).toHaveAttribute(
    "href",
    "/workflows/draft",
  );
  await expect(page.getByRole("heading", { level: 2, name: "Needs your decision" })).toBeVisible();

  // The navigation costs nothing until it is summoned: it is hidden behind the screen's left
  // edge, and pinning it keeps it standing for the rest of this walk.
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("button", { name: "Pin sidebar" }).click();
  const nav = page.getByRole("navigation", { name: "Stations" });
  await expect(nav).toBeVisible();
  // Main (Home, Chat) + Workflows (Fleet, Library, Schedules) + Workspace
  // (Projects, Knowledge bases) + System (Harness, Inspection, Settings) = 10 entries.
  await expect(nav.getByRole("link")).toHaveCount(10);

  // Chat is secondary now, and still one press away from anywhere. It names itself through its
  // own panes rather than a heading.
  await nav.getByRole("link", { name: "Chat" }).click();

  // Exact: a plain substring match for "Sessions" also hits "Loading sessions.",
  // transient text on this same first render. No `?session=` on first load, so no engine
  // mounts and the center pane is the composer alone.
  await expect(page.getByText("Sessions", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Pick a session on the left, or write the first message of a new one."),
  ).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();

  // The fleet board: one vertical stack, empty against the shared runs fixture.
  await nav.getByRole("link", { name: "Fleet" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Fleet" })).toBeVisible();
  await expect(page.getByText("Nothing running.", { exact: true })).toBeVisible();
  await expect(page.getByText("No runs to show yet.", { exact: true })).toBeVisible();

  // The follow-me rail: a collapsed strip until it is opened. The chat station hides it
  // entirely (it is already a chat), so open it here instead, on the fleet board.
  const companion = page.getByRole("button", { name: "Companion" }).first();
  await expect(companion).toBeVisible();
  await companion.click();
  await expect(nav.getByRole("link")).toHaveCount(10);

  // Each station: click its nav link (accessible names per the shared UI
  // contract), assert the station's <h1>.
  await nav.getByRole("link", { name: "Knowledge bases" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Knowledge bases" })).toBeVisible();
  await expect(page.getByText("research", { exact: true })).toBeVisible();

  // The graph lives inside a knowledge base's own page now, as its Graph tab. Mount only the tab,
  // never canvas internals.
  await page.getByRole("link", { name: "research" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "research" })).toBeVisible();
  await page.getByRole("tab", { name: "Graph" }).click();
  await expect(page.getByRole("combobox", { name: /find a page/i })).toBeVisible();

  await nav.getByRole("link", { name: "Harness" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Harness" })).toBeVisible();
  // The rail lives in the shell layout, not this page: what was opened on the fleet board
  // survives the route change.
  await expect(page.getByText("Companion", { exact: true }).first()).toBeVisible();

  // Back to chat to confirm the nav round-trips.
  await nav.getByRole("link", { name: "Chat" }).click();
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();
});
