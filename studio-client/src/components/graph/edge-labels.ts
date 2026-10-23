import type Sigma from "sigma";

// Edge labels drawn upright on a 2D canvas. sigma 4 lays an edge label along its edge
// from source to target, so the label of an edge drawn right to left comes out upside
// down (sigma.js issue 1544, open on sigma 4.0.3). The graph turns sigma's own edge
// labels off and draws them with this module instead, the way sigma 3 did.

const EDGE_LABEL_SIZE = 9;
const EDGE_LABEL_COLOR = "#94a3b8";
const EDGE_LABEL_FONT = `${EDGE_LABEL_SIZE}px sans-serif`;

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
 * Draws the edge labels sigma would show onto a 2D canvas the size of the graph.
 *
 * An edge is labelled when both its nodes show their labels or one of them is `focused`.
 * A label that does not fit between the node rims is left out.
 *
 * Args:
 *   sigma: The renderer whose graph, display data and camera the labels follow.
 *   context: The canvas to draw on. It is cleared first.
 *   focused: The hovered or selected node, or null.
 */
export function drawEdgeLabels(
  sigma: Sigma,
  context: CanvasRenderingContext2D,
  focused: string | null,
): void {
  const { width, height, pixelRatio } = sigma.getRenderParams();
  const canvas = context.canvas;
  if (canvas.width !== width * pixelRatio || canvas.height !== height * pixelRatio) {
    canvas.width = width * pixelRatio;
    canvas.height = height * pixelRatio;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
  }
  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, width, height);
  context.font = EDGE_LABEL_FONT;
  context.fillStyle = EDGE_LABEL_COLOR;

  const labelled = sigma.getNodeDisplayedLabels();
  sigma.getGraph().forEachEdge((edge, _attributes, source, target) => {
    const anchored =
      source === focused || target === focused || (labelled.has(source) && labelled.has(target));
    if (!anchored) return;
    const data = sigma.getEdgeDisplayData(edge);
    if (!data?.label || data.visibility === "hidden" || data.labelVisibility === "hidden") return;
    const sourceDisc = onScreen(sigma, source);
    const targetDisc = onScreen(sigma, target);
    if (sourceDisc === null || targetDisc === null) return;
    const placement = edgeLabelPlacement(sourceDisc, targetDisc);
    if (placement === null) return;
    const textWidth = context.measureText(data.label).width;
    if (textWidth > placement.length) return;
    context.save();
    context.translate(placement.x, placement.y);
    context.rotate(placement.angle);
    // Just under the line, clear of the edge's own stroke.
    context.fillText(data.label, -textWidth / 2, data.size / 2 + EDGE_LABEL_SIZE);
    context.restore();
  });
}

function onScreen(sigma: Sigma, node: string): Disc | null {
  const data = sigma.getNodeDisplayData(node);
  if (!data || data.visibility === "hidden") return null;
  const { x, y } = sigma.framedGraphToViewport(data);
  return { x, y, size: sigma.scaleSize(data.size) };
}
