import { type Page, type Route, expect, test } from "@playwright/test";

// The knowledge base graph drawn for real: sigma renders on WebGL, which jsdom cannot run, so this
// is the one check that the canvas paints nodes, edges and edge labels. Every endpoint the detail
// page fetches is mocked, the graph included. The canvas is measured from a screenshot decoded in
// the page, because a WebGL canvas cannot be read back once it has been shown.

const PAPER_BLUE: Rgb = [0x25, 0x63, 0xeb];
const CONCEPT_PURPLE: Rgb = [0x93, 0x33, 0xea];
const EDGE_GREY: Rgb = [0xd7, 0xde, 0xe8];
const NODE_LABEL_SLATE: Rgb = [0x33, 0x41, 0x55];
const EDGE_LABEL_SLATE: Rgb = [0x94, 0xa3, 0xb8];

type Rgb = [number, number, number];

// A box in CSS pixels, measured from the canvas center.
interface Band {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

// Twice the pixel density, so the labels in the saved screenshots stay legible.
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

// The graph canvas as RGBA pixels, `scale` device pixels to a CSS pixel.
interface CanvasPixels {
  width: number;
  height: number;
  scale: number;
  rgba: Buffer;
}

// A point in CSS pixels.
interface Point {
  x: number;
  y: number;
}

// The graph canvas, decoded once from a screenshot: the page draws the PNG on a 2D canvas and
// hands its pixels back.
async function canvasPixels(page: Page): Promise<CanvasPixels> {
  const png = await page.locator(".sigma-container").screenshot();
  const decoded = await page.evaluate(async (data) => {
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
    const url = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () =>
        typeof reader.result === "string" ? resolve(reader.result) : reject(new Error("no data"));
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(new Blob([pixels]));
    });
    return {
      width: canvas.width,
      height: canvas.height,
      scale: window.devicePixelRatio,
      base64: url.slice(url.indexOf(",") + 1),
    };
  }, png.toString("base64"));
  return { ...decoded, rgba: Buffer.from(decoded.base64, "base64") };
}

// Every pixel within `tolerance` of `colour` on every channel, in CSS pixels from the canvas's
// top left corner.
function* pixelsOf(pixels: CanvasPixels, colour: Rgb, tolerance: number): Generator<Point> {
  const [red, green, blue] = colour;
  for (let index = 0; index < pixels.width * pixels.height; index += 1) {
    const offset = index * 4;
    if (
      Math.abs(pixels.rgba[offset] - red) <= tolerance &&
      Math.abs(pixels.rgba[offset + 1] - green) <= tolerance &&
      Math.abs(pixels.rgba[offset + 2] - blue) <= tolerance
    ) {
      yield {
        x: (index % pixels.width) / pixels.scale,
        y: Math.floor(index / pixels.width) / pixels.scale,
      };
    }
  }
}

// Where the graph canvas sits on the page.
async function canvasOrigin(page: Page): Promise<Point> {
  const box = await page.locator(".sigma-container").boundingBox();
  if (box === null) throw new Error("no graph container");
  return { x: box.x, y: box.y };
}

// How many canvas pixels sit within `tolerance` of each colour, on every channel, in the whole
// canvas or in `band`.
async function countColours(
  page: Page,
  colours: Rgb[],
  tolerance: number,
  band?: Band,
): Promise<number[]> {
  const pixels = await canvasPixels(page);
  const centerX = pixels.width / pixels.scale / 2;
  const centerY = pixels.height / pixels.scale / 2;
  return colours.map((colour) => {
    let count = 0;
    for (const { x, y } of pixelsOf(pixels, colour, tolerance)) {
      if (band !== undefined) {
        if (x - centerX < band.left || x - centerX > band.right) continue;
        if (y - centerY < band.top || y - centerY > band.bottom) continue;
      }
      count += 1;
    }
    return count;
  });
}

// Where each node of `colour` sits on the page, from the top of the canvas down: the middle of
// each patch of that colour.
async function nodesOnPage(page: Page, colour: Rgb): Promise<Point[]> {
  const pixels = await canvasPixels(page);
  const origin = await canvasOrigin(page);
  const patches: Array<{ first: Point; sumX: number; sumY: number; count: number }> = [];
  for (const point of pixelsOf(pixels, colour, 8)) {
    const patch = patches.find(
      ({ first }) => Math.hypot(point.x - first.x, point.y - first.y) < 15,
    );
    if (patch === undefined) {
      patches.push({ first: point, sumX: point.x, sumY: point.y, count: 1 });
    } else {
      patch.sumX += point.x;
      patch.sumY += point.y;
      patch.count += 1;
    }
  }
  return patches
    .filter(({ count }) => count > 20)
    .map(({ sumX, sumY, count }) => ({ x: origin.x + sumX / count, y: origin.y + sumY / count }))
    .sort((one, other) => one.y - other.y);
}

// The node of `colour` nearest the top of the canvas, or the one nearest the bottom.
async function nodeOnPage(page: Page, colour: Rgb, end: "top" | "bottom"): Promise<Point> {
  const nodes = await nodesOnPage(page, colour);
  const node = end === "top" ? nodes.at(0) : nodes.at(-1);
  if (node === undefined) throw new Error("no node of that colour");
  return node;
}

// Dark label pixels just right of the canvas center once the camera has landed a node of
// `colour` there, which is where sigma draws the selected node's label. Zero until it lands.
async function selectedLabelPixels(page: Page, colour: Rgb): Promise<number> {
  const center = { left: -6, right: 6, top: -6, bottom: 6 };
  const [atCenter] = await countColours(page, [colour], 24, center);
  if (atCenter < 20) return 0;
  const band = { left: 4, right: 160, top: -14, bottom: 14 };
  const [pixels] = await countColours(page, [NODE_LABEL_SLATE], 24, band);
  return pixels;
}

// Edge label pixels. Nothing else is painted in that colour while no node is hovered or selected:
// the edges are lighter, and a node label's anti-aliasing passes it on a different blue.
async function edgeLabelPixels(page: Page): Promise<number> {
  const [pixels] = await countColours(page, [EDGE_LABEL_SLATE], 10);
  return pixels;
}

// Edge label pixels along the middle of the line between two points on the page, split by the
// side of the line they sit on: above it on screen, or below it. Pixels closer to the line than
// `gap` count on neither side, so a highlighted line, drawn in the label's colour, is left out.
async function labelSides(
  page: Page,
  from: Point,
  to: Point,
  gap = 1,
): Promise<{ above: number; below: number }> {
  const pixels = await canvasPixels(page);
  const origin = await canvasOrigin(page);
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const length = Math.hypot(dx, dy);
  // The unit normal that points down the screen.
  const flip = dx < 0 ? -1 : 1;
  const normalX = (-dy / length) * flip;
  const normalY = (dx / length) * flip;
  let above = 0;
  let below = 0;
  for (const point of pixelsOf(pixels, EDGE_LABEL_SLATE, 10)) {
    const x = origin.x + point.x - from.x;
    const y = origin.y + point.y - from.y;
    const along = (x * dx + y * dy) / (length * length);
    const across = x * normalX + y * normalY;
    if (along < 0.2 || along > 0.8 || Math.abs(across) > 16) continue;
    if (across < -gap) above += 1;
    else if (across > gap) below += 1;
  }
  return { above, below };
}

// Where a point sits on the page, for a failure message.
function at({ x, y }: Point): string {
  return `(${Math.round(x)}, ${Math.round(y)})`;
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

// The selected node is ringed and its label must read above the ring, however it was selected.
test("the selected node's label reads above its ring, after a click and after Find a page", async ({
  page,
}) => {
  await mockApi(page, { nodes: NODES, edges: EDGES });
  await openGraph(page);
  await expect
    .poll(async () => Math.min(...(await countColours(page, [PAPER_BLUE, CONCEPT_PURPLE], 24))), {
      timeout: 15_000,
    })
    .toBeGreaterThan(20);

  // A click on Paper A: the camera flies to it, and the pointer then leaves the canvas.
  const paper = await nodeOnPage(page, PAPER_BLUE, "top");
  await page.mouse.click(paper.x, paper.y);
  await page.mouse.move(0, 0);
  await expect(page.getByText("Paper A").last()).toBeVisible();
  await expect
    .poll(() => selectedLabelPixels(page, PAPER_BLUE), { timeout: 10_000 })
    .toBeGreaterThan(40);
  await page
    .locator(".sigma-container")
    .screenshot({ path: test.info().outputPath("selected-by-click.png") });

  // Concept C through the search box.
  await page.getByRole("combobox", { name: "Find a page" }).fill("Concept C");
  await page.getByRole("option", { name: /Concept C/ }).click();
  await page.mouse.move(0, 0);
  await expect
    .poll(() => selectedLabelPixels(page, CONCEPT_PURPLE), { timeout: 10_000 })
    .toBeGreaterThan(40);
  await page
    .locator(".sigma-container")
    .screenshot({ path: test.info().outputPath("selected-by-search.png") });
});

// The edge from Paper A to Concept C runs right to left. Laid from source to target its label
// would read upside down, and its below-the-line side would face up the screen.
test("an edge drawn right to left keeps its label upright, just under its line", async ({
  page,
}) => {
  await mockApi(page, { nodes: NODES, edges: EDGES });
  await openGraph(page);
  await expect.poll(() => edgeLabelPixels(page), { timeout: 15_000 }).toBeGreaterThan(50);

  // Paper A and Concept C are the topmost node of each colour.
  await expect
    .poll(
      async () => {
        const paper = await nodeOnPage(page, PAPER_BLUE, "top");
        const concept = await nodeOnPage(page, CONCEPT_PURPLE, "top");
        const measured = `from the topmost blue node ${at(paper)}, taken as Paper A, to the topmost purple node ${at(concept)}, taken as Concept C`;
        if (concept.x >= paper.x) return `the edge ${measured} does not run right to left`;
        const { above, below } = await labelSides(page, paper, concept);
        return below > 40 && above < below / 10
          ? "under the line"
          : `${above} above and ${below} below the line ${measured}`;
      },
      { timeout: 10_000 },
    )
    .toBe("under the line");
  await page
    .locator(".sigma-container")
    .screenshot({ path: test.info().outputPath("right-to-left-label.png") });
});

// A node picked by a click stays selected after the pointer leaves, and its edges keep their
// labels, even where the camera flies so close that the node at the other end is off screen.
test("a selected node keeps its edge labels after the pointer leaves", async ({ page }) => {
  await mockApi(page, { nodes: NODES, edges: EDGES });
  await openGraph(page);
  await expect.poll(() => edgeLabelPixels(page), { timeout: 15_000 }).toBeGreaterThan(50);

  // Paper B and Concept D are the lowest node of each colour, joined by Paper B's "defines" edge.
  const paper = await nodeOnPage(page, PAPER_BLUE, "bottom");
  const concept = await nodeOnPage(page, CONCEPT_PURPLE, "bottom");
  await page.mouse.click(paper.x, paper.y);
  await page.mouse.move(0, 0);
  await expect(page.getByText("Paper B").last()).toBeVisible();
  await expect
    .poll(() => selectedLabelPixels(page, PAPER_BLUE), { timeout: 10_000 })
    .toBeGreaterThan(40);

  // The camera lands Paper B at the canvas center at twice the zoom, which puts Concept D off
  // screen. The label sits beside the middle of the line, clear of the highlighted line itself.
  const origin = await canvasOrigin(page);
  const box = await page.locator(".sigma-container").boundingBox();
  if (box === null) throw new Error("no graph container");
  const center = { x: origin.x + box.width / 2, y: origin.y + box.height / 2 };
  const end = { x: center.x + 2 * (concept.x - paper.x), y: center.y + 2 * (concept.y - paper.y) };
  const measured = `beside the edge from the lowest blue node ${at(paper)}, taken as Paper B, to the lowest purple node ${at(concept)}, taken as Concept D`;
  await expect
    .poll(
      async () => {
        const { above, below } = await labelSides(page, center, end, 3);
        return above + below > 40 ? "labelled" : `${above + below} label pixels ${measured}`;
      },
      { timeout: 5_000 },
    )
    .toBe("labelled");
  await page
    .locator(".sigma-container")
    .screenshot({ path: test.info().outputPath("selected-keeps-edge-labels.png") });
});
