import { type Page, expect, test } from "@playwright/test";

import type { z } from "zod";

import type { RunViewSchema } from "@/api/sessions-types";
import type {
  ArtifactRefSchema,
  LibraryEntrySchema,
  RunEnvelopeSchema,
  WorkflowDetailSchema,
  WorkflowGraphSchema,
} from "@/api/studio-types";

import { DEFAULT_PORT, resolvePort } from "./port";

// The automation journey end to end, at a desktop width and at 390 px, with no backend: every
// request is answered here from one mutable journey, so the same spec can move a run from launched
// to waiting to ended. The shapes are annotated with the INPUT type of the schema the app parses
// them with, so a fixture that drifts from the contract fails the typecheck rather than the page.
//
// The whole walk is keyboard driven where a person would drive it: the primary action, the choice
// of a saved workflow, and the setup dialog are reached with Tab and pressed with Enter.

type Wire<S extends { _zod: { input: unknown } }> = z.input<S>;

const DESKTOP = { width: 1280, height: 900 };
const NARROW = { width: 390, height: 844 };

const WORKFLOW_ID = "wf.weekly-sweep.v2";
const RUN_TITLE = "Weekly sweep, the open queue";

interface Journey {
  /** The status the live pool answers with, or 0 for a normal answer. */
  liveStatus: number;
  runs: Wire<typeof RunViewSchema>[];
  recent: Wire<typeof RunEnvelopeSchema>[];
  launched: unknown[];
  decided: unknown[];
  asked: string[];
}

function freshJourney(): Journey {
  return { liveStatus: 0, runs: [], recent: [], launched: [], decided: [], asked: [] };
}

const LIBRARY: Wire<typeof LibraryEntrySchema>[] = [
  {
    archived_at: null,
    id: WORKFLOW_ID,
    title: "Weekly sweep",
    summary: "Checks the queue every week and writes up what changed.",
    provisional: false,
    trust: {
      approved_by: [],
      level: "unverified",
      runs_with_evidence: 0,
      failed_runs: 0,
      structural_grades: 0,
      structural_mean: null,
      structural_last: null,
    },
    needs: [],
    missing: [],
    brings: [],
    version: 2,
    shape: { steps: 2, person_gates: 1, agent_gates: 0 },
    runs: { last: null },
  },
];

const GRAPH: Wire<typeof WorkflowGraphSchema> = {
  name: "wf.weekly-sweep",
  version: 2,
  nodes: [
    {
      input_schemas: {},
      source: "",
      key: "topic",
      id: "topic",
      kind: "param",
      signature: "",
      detail: "Name the queue this run should sweep.",
      output_schema: { type: "string" },
      required: true,
    },
    {
      input_schemas: {},
      source: "",
      key: "review",
      id: "review",
      kind: "gate",
      signature: "",
      detail: "",
      output_schema: null,
      required: false,
    },
  ],
  edges: [{ from: "topic", to: "review", kind: "wire" }],
};

const ARTIFACT: Wire<typeof ArtifactRefSchema> = {
  id: "write-up",
  label: "The weekly write up",
  run_id: "run-1",
  node_id: "compose",
  turn: null,
  value_digest: "d".repeat(64),
  media_type: "application/json",
};

// The value that reference addresses: a note the run proposed, which is what a save_note action
// is offered over. Keeping it is the person's, and nothing here writes anything.
const PROPOSED_NOTE = {
  page_type: "decision-brief",
  title: "The weekly write up",
  summary: "The queue moved twice this week.",
  frontmatter: {},
  sections: [{ heading: "What changed", body: "Two items left the queue." }],
};

const WORKFLOW_DETAIL: Wire<typeof WorkflowDetailSchema> = {
  archived_at: null,
  ok: true,
  id: WORKFLOW_ID,
  name: WORKFLOW_ID,
  version: 2,
  description: "",
  title: "Weekly sweep",
  summary: "Checks the queue every week and writes up what changed.",
  applicability: "",
  provisional: false,
  graph: GRAPH,
  issues: [],
  fits: true,
  missing: [],
  brings: [],
  spec_yaml: "name: wf.weekly-sweep\n",
  staleness: [],
  lineage: null,
  birth: [],
  carried: null,
  predictions: null,
  prediction_record: null,
  trust: {
    approved_by: [],
    level: "unverified",
    runs_with_evidence: 0,
    failed_runs: 0,
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
  },
  presentation: {
    purpose: "Keeps the queue moving.",
    produces: "A short write up of what changed.",
    owner_participation: "You approve the write up before it lands.",
    not_for: "Anything that has to land the same hour.",
    inputs: [{ key: "topic", label: "What to sweep", control: "text" }],
    result_fields: [],
    actions: [
      {
        kind: "save_note",
        id: "keep-it",
        label: "Keep this write up",
        artifact_id: "write-up",
        destination: "user_selects",
      },
    ],
  },
};

function runningRun(): Wire<typeof RunViewSchema> {
  return {
    session_id: "s-new",
    workflow_id: WORKFLOW_ID,
    run_title: RUN_TITLE,
    state: "running",
    active: ["compose"],
    done: ["topic"],
    failed: [],
    waiting_at: null,
    gate: null,
    started_at: new Date().toISOString(),
  };
}

function waitingRun(): Wire<typeof RunViewSchema> {
  return {
    ...runningRun(),
    state: "waiting",
    active: [],
    waiting_at: "review",
    gate: {
      gate_id: "review",
      workflow: WORKFLOW_ID,
      reviewing: "The queue moved twice this week.",
      rejection_reruns: "compose",
    },
  };
}

function endedRun(): Wire<typeof RunEnvelopeSchema> {
  return {
    spend: null,
    run_id: "run-1",
    workflow_id: WORKFLOW_ID,
    run_title: RUN_TITLE,
    outcome: "completed",
    failure: "",
    started_at: "2026-09-12T09:00:00Z",
    ended_at: "2026-09-12T09:06:00Z",
    runner: "app:harness",
    session_id: "s-new",
    params: {},
    gates_approved: [],
    gates_rejected: {},
    gates_auto: [],
    gates_orchestrated: [],
    pinned: false,
    result: {
      status: "partial",
      summary: "One of the two sections came back empty.",
      attempted: 2,
      succeeded: 1,
      skipped: 0,
      failed: 1,
      retry_attempts: 0,
      primary_input: 2,
      generation: 0,
      checks: [],
      artifacts: [ARTIFACT],
    },
  };
}

function session(id: string, title: string) {
  return {
    id,
    project_id: "",
    title,
    started_at: "2026-09-12T09:00:00Z",
    last_activity_at: "2026-09-12T09:00:00Z",
    archived: false,
    answering: false,
    turn_count: 0,
    usage: { input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 },
    kind: "runner",
    cost_usd: "0",
    unpriced: 0,
    context: null,
  };
}

// One router for every request the walk can make, so nothing can reach a real backend: an
// unrecognized path answers with an empty object and is recorded, and the bounded-reads test
// asserts that Home asked for nothing outside its three reads.
async function mockApi(page: Page, journey: Journey): Promise<void> {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    journey.asked.push(`${method} ${path}${url.search}`);

    const json = (body: unknown) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

    if (path.startsWith("/api/events")) {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "retry: 100000\n\n",
      });
    }
    if (path === "/api/runs") {
      if (journey.liveStatus !== 0) {
        return route.fulfill({
          status: journey.liveStatus,
          contentType: "application/json",
          body: JSON.stringify({ detail: "the run pool is unreachable" }),
        });
      }
      return json({ runs: journey.runs });
    }
    if (path === "/api/runs/recent") return json({ runs: journey.recent });
    if (path === "/api/library") return json(LIBRARY);
    if (path === "/api/master") return json({ session_id: "m-1" });
    if (path === "/api/spend") {
      return json({
        available: false,
        scope: "step",
        subject: "run-1",
        since_hours: 24,
        reason: "no telemetry here",
        fallback: "the run's own ledger",
      });
    }
    if (path.startsWith("/api/workflow/")) return json(WORKFLOW_DETAIL);
    if (path.endsWith("/history") && path.startsWith("/api/workflows/")) {
      return json({ runs: journey.recent });
    }
    if (path.endsWith("/artifacts/resolve")) {
      return json({ ref: ARTIFACT, value: PROPOSED_NOTE });
    }
    if (path.startsWith("/api/runs/history/")) {
      return json({
        ...endedRun(),
        spec: {},
        values: {},
        conversations: {},
        owner_reviews: [],
        events: [],
        graph: GRAPH,
      });
    }
    if (path === "/api/sessions" && method === "POST") {
      return json({ session: session("s-new", "") });
    }
    if (path === "/api/sessions/s-new" && method === "PATCH") {
      return json({ session: session("s-new", "Weekly sweep") });
    }
    if (path === "/api/sessions/m-1") {
      return json({ session: { ...session("m-1", "master"), kind: "master" }, turns: [] });
    }
    if (path === "/api/sessions/s-new/run" && method === "POST") {
      journey.launched.push(request.postDataJSON());
      journey.runs = [runningRun()];
      return json({ ok: true, running: WORKFLOW_ID });
    }
    if (path.endsWith("/decide") && method === "POST") {
      journey.decided.push(request.postDataJSON());
      journey.runs = [];
      journey.recent = [endedRun()];
      return json({ ok: true, approved: false });
    }
    if (path === "/api/sessions") return json({ sessions: [] });
    return json({});
  });
}

/** The focusable element focus is on, by its accessible name, or "" for anything else. */
async function focusedName(page: Page): Promise<string> {
  return page.evaluate(() => {
    const element = document.activeElement;
    if (!(element instanceof HTMLElement)) return "";
    if (!element.matches("a, button, input, textarea, select")) return "";
    return (element.getAttribute("aria-label") ?? element.textContent ?? "").trim();
  });
}

/** Tabs until focus lands on the control with this accessible name, or fails saying it did not. */
async function tabTo(page: Page, name: string): Promise<void> {
  for (let press = 0; press < 25; press += 1) {
    if ((await focusedName(page)) === name) return;
    await page.keyboard.press("Tab");
  }
  throw new Error(`no control named ${name} was reachable by keyboard`);
}

/** A route this walk crosses for the first time is compiled on demand, so crossing one waits longer. */
const CROSSING = { timeout: 30_000 };

/**
 * How far the focused control's ring stands out from the page behind it, as a contrast ratio.
 *
 * `0` when there is no ring at all. The colour transitions in, so a reading taken the instant focus
 * lands is the colour on the way rather than the one that stays: poll this until it settles.
 */
async function focusRingContrast(page: Page): Promise<number> {
  return page.evaluate(() => {
    const channels = (color: string): [number, number, number, number] => {
      const parts = color.match(/[\d.]+/g)?.map(Number) ?? [];
      const [red = 0, green = 0, blue = 0, alpha = 1] = parts;
      return [red, green, blue, alpha];
    };
    const luminance = (color: [number, number, number, number]): number => {
      const channel = (value: number): number => {
        const part = value / 255;
        return part <= 0.03928 ? part / 12.92 : ((part + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2]);
    };

    const element = document.activeElement;
    if (!(element instanceof HTMLElement)) return 0;
    const style = window.getComputedStyle(element);
    if (style.outlineStyle === "none" || Number.parseFloat(style.outlineWidth) === 0) return 0;

    // The ring is painted outside the control, so what it has to stand out from is the first
    // ancestor that actually paints a background.
    let behind: HTMLElement | null = element.parentElement;
    let ground: [number, number, number, number] = [255, 255, 255, 1];
    while (behind !== null) {
      const color = channels(window.getComputedStyle(behind).backgroundColor);
      if (color[3] > 0) {
        ground = color;
        break;
      }
      behind = behind.parentElement;
    }

    const ring = luminance(channels(style.outlineColor));
    const page = luminance(ground);
    return (Math.max(ring, page) + 0.05) / (Math.min(ring, page) + 0.05);
  });
}

for (const size of [
  { label: "a desktop width", viewport: DESKTOP },
  { label: "390 px", viewport: NARROW },
]) {
  test.describe(size.label, () => {
    test.use({ viewport: size.viewport });

    test("goes from Home to a launched run, a decision, a result, and its artifact", async ({
      page,
    }) => {
      const journey = freshJourney();
      await mockApi(page, journey);
      await page.goto("/");

      await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();

      // The primary action is reachable by keyboard, shows where focus is, and opens the guided
      // route.
      await tabTo(page, "Create workflow");
      await expect.poll(() => focusRingContrast(page)).toBeGreaterThanOrEqual(3);
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/workflows\/draft$/, CROSSING);
      await page.goBack();
      await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible(CROSSING);

      // Choosing a saved workflow opens its setup, and setup is where focus goes.
      await tabTo(page, "Set up Weekly sweep");
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      expect(
        await page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null),
      ).toBe(true);

      // Backing out creates nothing and hands focus back to the control that opened it.
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect.poll(() => focusedName(page)).toBe("Set up Weekly sweep");
      expect(journey.launched).toHaveLength(0);

      // The gate policy belongs to setup, not to Home.
      await page.keyboard.press("Enter");
      await expect(dialog).toBeVisible();
      await expect(dialog.getByText("Gates on this run")).toBeVisible();

      // The setup is drawn from the workflow's own schema and the author's own label.
      const field = dialog.getByLabel("What to sweep");
      await expect(field).toBeVisible();
      await field.fill("the open queue");
      await tabTo(page, "Run it");
      await page.keyboard.press("Enter");

      await expect.poll(() => journey.launched.length).toBe(1);
      expect(journey.launched[0]).toEqual({
        workflow_id: WORKFLOW_ID,
        arguments: { topic: "the open queue" },
        run_title: RUN_TITLE,
      });

      // The launch stays on Home, and the run appears under what is active, by its own name.
      await expect(page).toHaveURL(/\/$/);
      const active = page.locator('[data-section="home-active"]');
      await expect(active.getByRole("link", { name: RUN_TITLE })).toBeVisible();
      await expect(active.getByText("Running", { exact: true })).toBeVisible();

      // The run reaches its gate, and the decision is answered where it is read.
      journey.runs = [waitingRun()];
      await page.reload();
      const decisions = page.locator('[data-section="home-decisions"]');
      await expect(
        decisions.locator('[data-section="title"]').getByText("Waiting on you", { exact: true }),
      ).toBeVisible();
      await decisions.getByRole("button", { name: "Reject with feedback" }).click();
      await decisions
        .getByRole("textbox", { name: "Feedback" })
        .fill("the second section is thin");
      await decisions.getByRole("button", { name: "Send rejection" }).click();

      await expect.poll(() => journey.decided.length).toBe(1);
      expect(journey.decided[0]).toEqual({
        approve: false,
        feedback: "the second section is thin",
      });

      // What it did, in words that do not claim more than was checked.
      await page.reload();
      const results = page.locator('[data-section="home-results"]');
      await expect(results.getByText("Partly done", { exact: true })).toBeVisible();
      await expect(results.getByText("1 of 2 succeeded, 0 skipped, 1 failed.")).toBeVisible();
      await expect(results.getByText("The weekly write up")).toBeVisible();

      // The run's own page reads the artifact through the run's own route.
      await results.getByRole("link", { name: RUN_TITLE }).click();
      await expect(page).toHaveURL(/run=run-1/, CROSSING);
      await expect(page.getByText("What this run left behind")).toBeVisible(CROSSING);
      await page.getByRole("button", { name: "Show" }).click();
      await expect(page.getByText("The queue moved twice this week.").first()).toBeVisible();

      // The declared action renders from its own closed intent, and this deployment carries it
      // out: one press resolves the run's own reference and opens the preview over that note.
      // Nothing is written by pressing it.
      const action = page.getByRole("button", { name: "Keep this write up" });
      await expect(action).toBeVisible();
      await expect(action).toBeEnabled();
      await action.click();
      const preview = page.getByRole("dialog");
      await expect(preview.getByText("Keep this as a note")).toBeVisible(CROSSING);
      await expect(preview.getByLabel("Title")).toHaveValue(PROPOSED_NOTE.title);
    });

    test("keeps Home, the fleet and a run inside the viewport with their primary act visible", async ({
      page,
    }) => {
      const journey = freshJourney();
      journey.runs = [waitingRun()];
      journey.recent = [endedRun()];
      await mockApi(page, journey);

      for (const route of ["/", "/workflows", `/workflows/${WORKFLOW_ID}?run=run-1`]) {
        await page.goto(route);
        await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();

        const fits = await page.evaluate(() => ({
          document: document.documentElement.scrollWidth,
          viewport: window.innerWidth,
        }));
        expect(fits.document).toBeLessThanOrEqual(fits.viewport);

        const pane = await page
          .locator("main")
          .evaluate((element) => ({ scroll: element.scrollWidth, client: element.clientWidth }));
        expect(pane.scroll).toBeLessThanOrEqual(pane.client);
      }

      // Home's primary act is whole on screen at either width.
      await page.goto("/");
      const create = page.getByRole("link", { name: "Create workflow" });
      const box = await create.boundingBox();
      expect(box).not.toBeNull();
      expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(size.viewport.width);
    });

    test("says nothing is waiting only when the live pool actually answered", async ({ page }) => {
      const journey = freshJourney();
      journey.liveStatus = 503;
      await mockApi(page, journey);
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();

      const active = page.locator('[data-section="home-active"]');
      const decisions = page.locator('[data-section="home-decisions"]');
      await expect(active.getByRole("alert")).toContainText("Could not read the runs in flight.");
      await expect(decisions.getByRole("alert")).toContainText(
        "Could not read what is waiting on you.",
      );
      await expect(active.getByText("Nothing is running.")).toBeHidden();
      await expect(decisions.getByText("No decision is waiting.")).toBeHidden();
      await expect(page.getByText("Nothing is running right now.")).toBeHidden();
      await expect(page.locator("main [aria-live]")).toHaveText(
        "Could not read what is running.",
      );
    });

    test("puts the card's own act where a hand is, without moving it in the document", async ({
      page,
    }) => {
      const journey = freshJourney();
      journey.recent = [endedRun()];
      await mockApi(page, journey);
      await page.goto("/");

      const card = page.locator('[data-section="home-results"] li').first();
      const act = card.locator('[data-section="action"] button');
      await expect(act).toBeVisible();

      const cardBox = await card.boundingBox();
      const actBox = await act.boundingBox();
      expect(cardBox).not.toBeNull();
      expect(actBox).not.toBeNull();
      const leading = (actBox?.x ?? 0) - (cardBox?.x ?? 0);
      const trailing =
        (cardBox?.x ?? 0) + (cardBox?.width ?? 0) - ((actBox?.x ?? 0) + (actBox?.width ?? 0));
      if (size.viewport.width >= 640) {
        expect(trailing).toBeLessThan(leading);
      } else {
        expect(leading).toBeLessThan(trailing);
      }

      // Wherever it sits, it still reads before the execution facts.
      const order = await card.evaluate((element) =>
        Array.from(element.querySelectorAll("[data-section]")).map((node) =>
          node.getAttribute("data-section"),
        ),
      );
      expect(order.indexOf("action")).toBeLessThan(order.indexOf("execution"));
    });

    test("reads in one heading order, and says every state in words beside its colour", async ({
      page,
    }) => {
      const journey = freshJourney();
      journey.runs = [waitingRun()];
      journey.recent = [endedRun()];
      await mockApi(page, journey);
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();

      const headings = await page.evaluate(() =>
        Array.from(document.querySelectorAll("main h1, main h2, main h3")).map(
          (node) => `${node.tagName} ${node.textContent?.trim() ?? ""}`,
        ),
      );
      expect(headings).toEqual([
        "H1 Home",
        "H2 Start automation",
        "H2 Active automations",
        "H2 Needs your decision",
        "H2 Recent results",
      ]);

      // A state is a word, never a colour on its own.
      await expect(page.getByText("Waiting on you", { exact: true }).first()).toBeVisible();
      await expect(page.getByText("Partly done", { exact: true })).toBeVisible();
      await expect(page.getByText("Completed", { exact: true })).toBeVisible();

      // One live region on the screen, and it is polite.
      const live = await page.evaluate(() =>
        Array.from(document.querySelectorAll("main [aria-live]")).map((node) =>
          node.getAttribute("aria-live"),
        ),
      );
      expect(live).toEqual(["polite"]);
    });
  });
}

test.describe("what Home asks for", () => {
  test.use({ viewport: DESKTOP });

  test("reads the live pool, the saved workflows, and a bounded page of results", async ({
    page,
  }) => {
    const journey = freshJourney();
    await mockApi(page, journey);
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Home" })).toBeVisible();
    // Every read has answered by the time the library's own card and the empty archive are drawn.
    await expect(page.getByRole("button", { name: "Set up Weekly sweep" })).toBeVisible();
    await expect(page.getByText("No results yet.")).toBeVisible();
    await expect.poll(() => journey.asked.filter((asked) => asked.startsWith("GET /api/")).length)
      .toBeGreaterThanOrEqual(3);

    const reads = journey.asked.filter((asked) => asked.startsWith("GET /api/"));
    expect(reads).toContain("GET /api/runs");
    expect(reads).toContain("GET /api/library");
    expect(reads).toContain("GET /api/runs/recent?limit=6");

    // No per-card graph read, and no unbounded archive read.
    expect(reads.filter((asked) => asked.startsWith("GET /api/workflow/"))).toEqual([]);
    expect(reads).not.toContain("GET /api/runs/recent");
  });
});

test.describe("the port an end to end run serves on", () => {
  test("defaults to the always-on dev server and refuses anything that is not a port", () => {
    expect(resolvePort(undefined)).toBe(DEFAULT_PORT);
    expect(resolvePort("")).toBe(DEFAULT_PORT);
    expect(resolvePort("3131")).toBe(3131);
    for (const bad of ["no", "31a31", "0", "70000", "3131.5"]) {
      expect(() => resolvePort(bad)).toThrow(/must be a port number/);
    }
  });
});
