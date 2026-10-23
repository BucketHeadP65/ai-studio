import { type Page, type Route, expect, test } from "@playwright/test";

// The knowledge base graph drawn for real: sigma renders on WebGL, which jsdom cannot run, so this
// is the one check that the canvas paints nodes, edges and edge labels. Every endpoint the detail
// page fetches is mocked, the graph included. The canvas is measured from a screenshot decoded in
// the page, because a WebGL canvas cannot be read back once it has been shown. The edge labels sit
// on their own 2D canvas, which can be read back directly.

const PAPER_BLUE: Rgb = [0x25, 0x63, 0xeb];
const CONCEPT_PURPLE: Rgb = [0x93, 0x33, 0xea];
const EDGE_GREY: Rgb = [0xd7, 0xde, 0xe8];

type Rgb = [number, number, number];

// Twice the pixel density, so the 9px edge labels in the saved screenshots stay legible.
test.use({ deviceScaleFactor: 2 });

const NODES = [
  { id: "paper:a", type: "paper", title: "Paper A", status: "verified", superseded: false },
  { id: "paper:b", type: "paper", title: "Paper B", status: "verified", superseded: false },
  { id: "concept:c", type: "concept", title: "Concept C", status: "draft", superseded: false },
  { id: "concept:d", type: "concept", title: "Concept D", status: "draft", superseded: false },
];

const EDGES = [
  { src_id: "paper:a", dst_id: "concept:c", rel: "defines", provenance_claim: null },
  { src_id: "paper:b", dst_id: "concept:c", rel: "uses", provenance_claim: null },
  { src_id: "paper:a", dst_id: "paper:b", rel: "cites", provenance_claim: null },
  { src_id: "paper:b", dst_id: "concept:d", rel: "defines", provenance_claim: null },
];

const KNOWLEDGE_BASE = {
  name: "research",
  registered: true,
  indexed: true,
  broken: false,
  path: "/knowledge-bases/research",
  description: "",
  pages: 4,
  page_types: ["paper", "concept"],
  last_indexed: "2026-08-11T09:00:00+00:00",
};

function json(route: Route, body: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockApi(page: Page, graph: { nodes: unknown[]; edges: unknown[] }) {
  // Registered first, so every route below wins over it: anything else the shell asks for answers
  // empty instead of hanging the page.
  await page.route("**/api/**", (route) => json(route, {}));
  await page.route("**/api/events**", (route) =>
    route.fulfill({ status: 200, contentType: "text/event-stream", body: "retry: 100000\n\n" }),
  );
  await page.route("**/api/graph**", (route) => json(route, graph));
  await page.route("**/api/knowledge-bases", (route) =>
    json(route, { knowledge_bases: [KNOWLEDGE_BASE] }),
  );
  await page.route("**/api/knowledge-bases/research", (route) =>
    json(route, {
      ...KNOWLEDGE_BASE,
      sources_connectors: [],
      watch_list: [],
      validation: [],
      source_queue: [],
      last_refresh_run: null,
      schedule: null,
    }),
  );
  await page.route("**/api/knowledge-bases/research/runs", (route) => json(route, { runs: [] }));
  await page.route("**/api/knowledge-bases/research/decisions", (route) =>
    json(route, { rows: [], skipped: [], outcomes_by_page_type: {} }),
  );
  await page.route("**/api/master", (route) => json(route, { session_id: null }));
  await page.route("**/api/sessions**", (route) => json(route, { sessions: [] }));
  await page.route("**/api/runs**", (route) => json(route, { runs: [] }));
}

// How many canvas pixels sit within `tolerance` of each colour, on every channel.
async function countColours(page: Page, colours: Rgb[], tolerance: number): Promise<number[]> {
  const png = await page.locator(".sigma-container").screenshot();
  return page.evaluate(
    async ({ data, wanted, slack }) => {
      const image = new Image();
      image.src = `data:image/png;base64,${data}`;
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = image.width;
      canvas.height = image.height;
      const context = canvas.getContext("2d");
      if (context === null) throw new Error("no 2d context");
      context.drawImage(image, 0, 0);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      const counts = wanted.map(() => 0);
      for (let offset = 0; offset < pixels.length; offset += 4) {
        wanted.forEach(([red, green, blue], index) => {
          if (
            Math.abs(pixels[offset] - red) <= slack &&
            Math.abs(pixels[offset + 1] - green) <= slack &&
            Math.abs(pixels[offset + 2] - blue) <= slack
          ) {
            counts[index] += 1;
          }
        });
      }
      return counts;
    },
    { data: png.toString("base64"), wanted: colours, slack: tolerance },
  );
}

// How many pixels the edge label layer painted.
async function edgeLabelPixels(page: Page): Promise<number> {
  return page.locator("canvas.sigma-edge-labels").evaluate((canvas: HTMLCanvasElement) => {
    const context = canvas.getContext("2d");
    if (context === null) throw new Error("no 2d context");
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
    let painted = 0;
    for (let offset = 3; offset < pixels.length; offset += 4) {
      if (pixels[offset] > 0) painted += 1;
    }
    return painted;
  });
}

async function openGraph(page: Page) {
  await page.goto("/knowledge-bases/research");
  await expect(page.getByRole("heading", { level: 1, name: "research" })).toBeVisible();
  await page.getByRole("tab", { name: "Graph" }).click();
  await expect(page.locator(".sigma-container canvas").first()).toBeVisible();
}

test("the graph tab draws the nodes in their type colours, the edges and their labels", async ({
  page,
}) => {
  await mockApi(page, { nodes: NODES, edges: EDGES });
  await openGraph(page);

  // Both node types are painted, each in its own colour.
  await expect
    .poll(async () => Math.min(...(await countColours(page, [PAPER_BLUE, CONCEPT_PURPLE], 24))), {
      timeout: 15_000,
    })
    .toBeGreaterThan(20);
  await expect.poll(() => edgeLabelPixels(page), { timeout: 15_000 }).toBeGreaterThan(50);
  await page.screenshot({ path: test.info().outputPath("graph.png") });
  await page
    .locator(".sigma-container")
    .screenshot({ path: test.info().outputPath("graph-canvas.png") });
  const [withEdges] = await countColours(page, [EDGE_GREY], 6);

  // The same nodes with no edges: whatever the edge colour still matches is label anti-aliasing,
  // so the edges are what the difference measures.
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await mockApi(page, { nodes: NODES, edges: [] });
  await openGraph(page);
  await expect
    .poll(async () => Math.min(...(await countColours(page, [PAPER_BLUE, CONCEPT_PURPLE], 24))), {
      timeout: 15_000,
    })
    .toBeGreaterThan(20);
  const [withoutEdges] = await countColours(page, [EDGE_GREY], 6);
  expect(await edgeLabelPixels(page)).toBe(0);

  expect(withEdges).toBeGreaterThan(withoutEdges + 100);
});
