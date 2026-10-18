import type { GraphNode, JsonSchema, WorkflowGraph } from "@/api/studio-types";

// Pure graph-shape helpers over a workflow's own graph: the order its steps run in, which of
// them could run side by side, its coarse shape counts (the same reading the library's own listing
// agrees on), what wires into a node, where a gate's decision goes, and reading a node's JSON
// Schema. None of these touch the network -- every caller passes the graph (or schema) it
// already has. Edges reference a node by its `key`, not its `id`: a param's own key carries the
// `param:` prefix its id does not, so a param source and a node source are told apart by the
// prefix alone.

const PARAM_PREFIX = "param:";

export function isJsonRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function relevantEdges(graph: WorkflowGraph) {
  return graph.edges.filter((edge) => edge.kind === "wire" || edge.kind === "map");
}

// The non param nodes in the order they run: Kahn's algorithm over `wire` and `map` edges only
// (`rejected` edges are loops, never a run order). Ties break by a node's own position in
// `graph.nodes`. A node no source ever reaches (a cycle among `rejected` aside) still appears,
// at the end, in its original order.
export function executionOrder(graph: WorkflowGraph): GraphNode[] {
  const indexOf = new Map(graph.nodes.map((node, index) => [node.key, index]));
  const byKey = new Map(graph.nodes.map((node) => [node.key, node]));
  const inDegree = new Map(graph.nodes.map((node) => [node.key, 0]));
  const outgoing = new Map<string, string[]>(graph.nodes.map((node) => [node.key, []]));

  for (const edge of relevantEdges(graph)) {
    if (!byKey.has(edge.from) || !byKey.has(edge.to)) continue;
    outgoing.get(edge.from)?.push(edge.to);
    inDegree.set(edge.to, (inDegree.get(edge.to) ?? 0) + 1);
  }

  const byIndex = (a: string, b: string) => (indexOf.get(a) ?? 0) - (indexOf.get(b) ?? 0);
  const queue = graph.nodes.filter((node) => inDegree.get(node.key) === 0).map((node) => node.key);
  queue.sort(byIndex);

  const visited: string[] = [];
  while (queue.length > 0) {
    queue.sort(byIndex);
    const key = queue.shift();
    if (key === undefined) break;
    visited.push(key);
    for (const next of outgoing.get(key) ?? []) {
      const remaining = (inDegree.get(next) ?? 0) - 1;
      inDegree.set(next, remaining);
      if (remaining === 0) queue.push(next);
    }
  }

  const visitedSet = new Set(visited);
  const ordered = visited
    .map((key) => byKey.get(key))
    .filter((node): node is GraphNode => node !== undefined);
  const leftover = graph.nodes.filter((node) => !visitedSet.has(node.key));

  return [...ordered, ...leftover].filter((node) => node.kind !== "param");
}

// Groups of nodes that could run side by side: same depth, depth = 1 + the deepest upstream
// node's own depth over `wire` and `map` edges, a param always at depth 0 and never itself in
// the result.
export function levels(graph: WorkflowGraph): GraphNode[][] {
  const byKey = new Map(graph.nodes.map((node) => [node.key, node]));
  const incoming = new Map<string, string[]>(graph.nodes.map((node) => [node.key, []]));
  for (const edge of relevantEdges(graph)) {
    if (!byKey.has(edge.from) || !byKey.has(edge.to)) continue;
    incoming.get(edge.to)?.push(edge.from);
  }

  const depthOf = new Map<string, number>();
  function depth(key: string, seen: Set<string>): number {
    const node = byKey.get(key);
    if (node?.kind === "param") return 0;
    const cached = depthOf.get(key);
    if (cached !== undefined) return cached;
    if (seen.has(key)) return 0;
    seen.add(key);
    const ups = incoming.get(key) ?? [];
    const upstreamMax = ups.reduce((max, upKey) => Math.max(max, depth(upKey, seen)), 0);
    const result = upstreamMax + 1;
    depthOf.set(key, result);
    return result;
  }

  const groups = new Map<number, GraphNode[]>();
  for (const node of graph.nodes) {
    if (node.kind === "param") continue;
    const d = depth(node.key, new Set());
    const list = groups.get(d) ?? [];
    list.push(node);
    groups.set(d, list);
  }

  return [...groups.keys()].sort((a, b) => a - b).map((d) => groups.get(d) ?? []);
}

export interface ShapeCounts {
  steps: number;
  person_gates: number;
  agent_gates: number;
}

// The same shape rule the library's own listing uses: a `gate` is a person gate, a `judge` or
// `orchestrator-gate` is an agent gate, a `param` is skipped, everything else is a step.
export function shapeCounts(graph: WorkflowGraph): ShapeCounts {
  let steps = 0;
  let personGates = 0;
  let agentGates = 0;
  for (const node of graph.nodes) {
    if (node.kind === "param") continue;
    if (node.kind === "gate") personGates += 1;
    else if (node.kind === "judge" || node.kind === "orchestrator-gate") agentGates += 1;
    else steps += 1;
  }
  return { steps, person_gates: personGates, agent_gates: agentGates };
}

export interface WireInto {
  arg: string;
  from: string;
  fromKind: "parameter" | "node";
  each: boolean;
}

// One entry per incoming `wire` or `map` edge into `nodeId`. `arg` is the text before the edge
// label's first colon (a `map` edge's label reads `each <arg>: ...`, the `each ` is stripped
// first). `from` is the source id with any `param:` prefix removed; `fromKind` says which it was.
export function wiresInto(graph: WorkflowGraph, nodeId: string): WireInto[] {
  return graph.edges
    .filter((edge) => edge.to === nodeId && (edge.kind === "wire" || edge.kind === "map"))
    .map((edge) => {
      const each = edge.kind === "map";
      const isParam = edge.from.startsWith(PARAM_PREFIX);
      const from = isParam ? edge.from.slice(PARAM_PREFIX.length) : edge.from;
      let labelBody = edge.label;
      if (each && labelBody.startsWith("each ")) {
        labelBody = labelBody.slice("each ".length);
      }
      const colonIndex = labelBody.indexOf(":");
      const arg = colonIndex === -1 ? labelBody : labelBody.slice(0, colonIndex);
      return { arg, from, fromKind: isParam ? "parameter" : "node", each } as const;
    });
}

// The node id a gate's rejection reruns from, or `null` when the gate carries no `rejected` edge.
export function rejectedTo(graph: WorkflowGraph, gateId: string): string | null {
  const edge = graph.edges.find(
    (candidate) => candidate.from === gateId && candidate.kind === "rejected",
  );
  return edge?.to ?? null;
}

// The node ids a gate's approval passes on to, off its own `wire` edges -- empty when the gate
// is the run's own output and nothing sits downstream of it.
export function passesTo(graph: WorkflowGraph, gateId: string): string[] {
  return graph.edges
    .filter((edge) => edge.from === gateId && edge.kind === "wire")
    .map((edge) => edge.to);
}

const DEFS_REF = /^#\/\$defs\/(.+)$/;

// Resolves a `{"$ref": "#/$defs/Name"}` schema against `root`'s own `$defs`, or returns `schema`
// itself when it carries no `$ref`. Falls back to `schema` when the ref cannot be resolved,
// rather than throwing on a schema this app does not fully understand.
export function resolveRef(schema: JsonSchema, root: JsonSchema): JsonSchema {
  const ref = schema.$ref;
  if (typeof ref !== "string") return schema;
  const match = DEFS_REF.exec(ref);
  if (match === null) return schema;
  const defs = root.$defs;
  if (!isJsonRecord(defs)) return schema;
  const resolved = defs[match[1]];
  return isJsonRecord(resolved) ? resolved : schema;
}

function isNullType(option: unknown): boolean {
  return isJsonRecord(option) && option.type === "null";
}

// The shape inside pydantic's optional wrapper: `anyOf` of exactly one shape and `null` reads as
// that shape alone. Any other schema comes back as it is.
export function unwrapNullable(schema: JsonSchema): JsonSchema {
  const anyOf = schema.anyOf;
  if (!Array.isArray(anyOf)) return schema;
  const shapes = anyOf.filter((option) => !isNullType(option));
  return shapes.length === 1 && isJsonRecord(shapes[0]) ? shapes[0] : schema;
}

// The schema a child's `$ref`s resolve against: the child itself when it carries `$defs` of its
// own (a wrapped item schema does), otherwise the root it sits under.
export function rootFor(child: JsonSchema, root: JsonSchema): JsonSchema {
  return isJsonRecord(child.$defs) ? child : root;
}

// A titled schema names a shape only when it describes an object. pydantic also titles every
// primitive field after the field's own name ("Item Type"), which is a label, not a type.
function describesObject(schema: JsonSchema): boolean {
  return schema.type === "object" || isJsonRecord(schema.properties);
}

// A one word (or one phrase) reading of a schema's own type: its `title` when it names an object,
// `"list of " + typeLabel(items)` for an array, otherwise its bare `type`. `anyOf` of a shape and
// `null` reads as that shape alone -- the optional wrapper pydantic emits around a nullable
// field, not a shape of its own. `root` resolves any `$ref` the schema (or its nested schemas)
// carries; a caller with no larger schema to resolve against may omit it, the schema resolves
// against its own `$defs` (if it has any) instead.
export function typeLabel(schema: JsonSchema, root: JsonSchema = schema): string {
  const resolved = resolveRef(schema, root);
  const title = resolved.title;
  if (typeof title === "string" && title !== "" && describesObject(resolved)) return title;

  const unwrapped = unwrapNullable(resolved);
  if (unwrapped !== resolved) return typeLabel(unwrapped, root);

  const type = resolved.type;
  if (type === "array") {
    const items = resolved.items;
    return isJsonRecord(items)
      ? `list of ${typeLabel(items, rootFor(items, root))}`
      : "list of object";
  }
  if (
    type === "string" ||
    type === "integer" ||
    type === "number" ||
    type === "boolean" ||
    type === "object"
  ) {
    return type;
  }
  return "object";
}
