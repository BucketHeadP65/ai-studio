import type Sigma from "sigma";

// Edge labels drawn upright on a 2D canvas. sigma 4 lays an edge label along its edge
// from source to target, so the label of an edge drawn right to left comes out upside
// down (sigma.js issue 1544, open on sigma 4.0.3). The graph turns sigma's own edge
// labels off and draws them with this module instead, the way sigma 3 did.

const EDGE_LABEL_SIZE = 9;
const EDGE_LABEL_COLOR = "#94a3b8";
const EDGE_LABEL_FONT = `${EDGE_LABEL_SIZE}px sans-serif`;

type Graph = ReturnType<Sigma["getGraph"]>;

/** A node as drawn on screen: its center and its radius, in viewport pixels. */
export interface Disc {
  x: number;
  y: number;
  size: number;
}

/** Where an edge label goes, in viewport pixels. */
export interface EdgeLabelPlacement {
  /** The middle of the stretch of edge between the two node rims. */
  x: number;
  y: number;
  /** The text's turn in radians, never more than a quarter turn either way. */
  angle: number;
  /** The length of that stretch, the room the label has. */
  length: number;
}

/**
 * Places an edge label on the stretch between the node rims, turned to read left to right.
 *
 * Args:
 *   source: The edge's source node on screen.
 *   target: The edge's target node on screen.
 *
 * Returns:
 *   The placement, or null when the two nodes overlap and no edge shows between them.
 */
export function edgeLabelPlacement(source: Disc, target: Disc): EdgeLabelPlacement | null {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.hypot(dx, dy);
  const length = distance - source.size - target.size;
  if (length <= 0) return null;
  const middle = source.size + length / 2;
  // Turned half a turn when the edge points left, so the text reads left to right.
  let angle = Math.atan2(dy, dx);
  if (angle > Math.PI / 2) angle -= Math.PI;
  else if (angle <= -Math.PI / 2) angle += Math.PI;
  return {
    x: source.x + (dx * middle) / distance,
    y: source.y + (dy * middle) / distance,
    angle,
    length,
  };
}

/**
 * Lists the edges whose labels show: both nodes show their labels, or one node is `focused`.
 *
 * Only the edges of those nodes are visited, never the whole graph.
 *
 * Args:
 *   graph: The graph the edges belong to.
 *   labelled: The nodes whose labels are drawn.
 *   focused: The hovered or selected node, or null.
 *
 * Returns:
 *   The edge keys, each once.
 */
export function labelledEdges(
  graph: Graph,
  labelled: ReadonlySet<string>,
  focused: string | null,
): Set<string> {
  const edges = new Set<string>();
  if (focused !== null && graph.hasNode(focused)) {
    graph.forEachEdge(focused, (edge) => {
      edges.add(edge);
    });
  }
  for (const node of labelled) {
    if (!graph.hasNode(node)) continue;
    graph.forEachEdge(node, (edge, _attributes, source, target) => {
      if (labelled.has(source === node ? target : source)) edges.add(edge);
    });
  }
  return edges;
}

/**
 * Draws the labels of the edges `labelledEdges` lists onto a 2D canvas the size of the graph.
 *
 * The canvas is resized to the render size when it differs, then cleared. An edge that is
 * hidden, has an empty label or a hidden label, or has a hidden node, is skipped. So is a
 * label that does not fit between the node rims.
 *
 * Args:
 *   sigma: The renderer whose graph, display data and camera the labels follow.
 *   context: The canvas to draw on.
 *   focused: The hovered or selected node, or null.
 */
export function drawEdgeLabels(
  sigma: Sigma,
  context: CanvasRenderingContext2D,
  focused: string | null,
): void {
  const { width, height, pixelRatio } = sigma.getRenderParams();
  if (fitCanvas(context.canvas, width, height, pixelRatio)) {
    context.canvas.style.width = `${width}px`;
    context.canvas.style.height = `${height}px`;
  }
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, width, height);
  context.font = EDGE_LABEL_FONT;
  context.fillStyle = EDGE_LABEL_COLOR;

  const graph = sigma.getGraph();
  for (const edge of labelledEdges(graph, sigma.getNodeDisplayedLabels(), focused)) {
    const data = sigma.getEdgeDisplayData(edge);
    if (!data?.label || data.visibility === "hidden" || data.labelVisibility === "hidden") continue;
    const [source, target] = graph.extremities(edge);
    const sourceDisc = onScreen(sigma, source);
    const targetDisc = onScreen(sigma, target);
    if (sourceDisc === null || targetDisc === null) continue;
    const placement = edgeLabelPlacement(sourceDisc, targetDisc);
    if (placement === null) continue;
    const textWidth = context.measureText(data.label).width;
    if (textWidth > placement.length) continue;
    context.save();
    context.translate(placement.x, placement.y);
    context.rotate(placement.angle);
    // Just under the line, clear of the edge's own stroke.
    context.fillText(data.label, -textWidth / 2, data.size / 2 + EDGE_LABEL_SIZE);
    context.restore();
  }
}

/**
 * Sizes a canvas's pixel buffer to a CSS size at a pixel ratio, when it differs.
 *
 * Args:
 *   canvas: The canvas to size.
 *   width: The width in CSS pixels.
 *   height: The height in CSS pixels.
 *   pixelRatio: Device pixels per CSS pixel.
 *
 * Returns:
 *   Whether the canvas was resized, which also clears it.
 */
export function fitCanvas(
  canvas: HTMLCanvasElement,
  width: number,
  height: number,
  pixelRatio: number,
): boolean {
  // Whole pixels: a canvas truncates a fraction, which would never compare equal again.
  const pixelWidth = Math.round(width * pixelRatio);
  const pixelHeight = Math.round(height * pixelRatio);
  if (canvas.width === pixelWidth && canvas.height === pixelHeight) return false;
  canvas.width = pixelWidth;
  canvas.height = pixelHeight;
  return true;
}

function onScreen(sigma: Sigma, node: string): Disc | null {
  const data = sigma.getNodeDisplayData(node);
  if (!data || data.visibility === "hidden") return null;
  const { x, y } = sigma.framedGraphToViewport(data);
  return { x, y, size: sigma.scaleSize(data.size) };
}
