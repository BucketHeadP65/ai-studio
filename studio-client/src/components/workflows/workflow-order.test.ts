import { describe, expect, it } from "vitest";

import type { GraphNode, WorkflowGraph } from "@/api/studio-types";
import {
  executionOrder,
  isJsonRecord,
  levels,
  passesTo,
  rejectedTo,
  resolveRef,
  rootFor,
  shapeCounts,
  typeLabel,
  unwrapNullable,
  wiresInto,
} from "./workflow-order";

// These pure helpers read a workflow's own graph the same way the backend's library listing and
// the "what it does" page both do -- run order, parallel levels, shape counts, what wires into a
// node, a gate's own two exits, and a node's JSON Schema. No network, no rendering.

function node(overrides: Partial<GraphNode> & Pick<GraphNode, "id" | "kind">): GraphNode {
  return {
    key: overrides.id,
    signature: "",
    required: true,
    detail: "",
    tools: [],
    is_output: false,
    unknown: false,
    brought: false,
    briefing: "",
    input_schemas: {},
    output_schema: null,
    source: "",
    ...overrides,
  };
}

// The research digest workflow from the real backend, with a `references` node added parallel
// to `digests` so the side by side level has two members to name.
const RESEARCH_GRAPH: WorkflowGraph = {
  name: "research-digest",
  version: 1,
  nodes: [
    node({ key: "param:collection", id: "collection", kind: "param", signature: "string" }),
    node({ id: "undigested", kind: "run" }),
    node({
      id: "digests",
      kind: "map",
      tools: ["read_source", "write_note"],
      brought: true,
      briefing: "Read the whole mirror with read_source.",
      work: "digest_source",
    }),
    node({ id: "references", kind: "map", work: "find_references" }),
    node({ id: "report", kind: "run", is_output: true, work: "compose_report" }),
  ],
  edges: [
    { from: "param:collection", to: "undigested", kind: "wire", label: "collection: string" },
    { from: "undigested", to: "digests", kind: "map", label: "each source: DigestResult" },
    { from: "undigested", to: "references", kind: "map", label: "each source: Reference" },
    { from: "digests", to: "report", kind: "wire", label: "digests: list[DigestResult]" },
    { from: "references", to: "report", kind: "wire", label: "references: list[Reference]" },
  ],
};

describe("executionOrder", () => {
  it("orders non param nodes by Kahn's algorithm over wire and map edges, params excluded", () => {
    const order = executionOrder(RESEARCH_GRAPH).map((n) => n.id);
    expect(order[0]).toBe("undigested");
    expect(order.slice(1, 3).sort()).toEqual(["digests", "references"]);
    expect(order[3]).toBe("report");
  });

  it("breaks ties by a node's own position in graph.nodes", () => {
    // digests sits before references in graph.nodes, both ready at the same time.
    const order = executionOrder(RESEARCH_GRAPH).map((n) => n.id);
    const digestsIndex = order.indexOf("digests");
    const referencesIndex = order.indexOf("references");
    expect(digestsIndex).toBeLessThan(referencesIndex);
  });

  it("ignores rejected edges, which are loops, not a run order", () => {
    const graph: WorkflowGraph = {
      name: "gated",
      version: 1,
      nodes: [
        node({ id: "compose", kind: "run" }),
        node({ id: "approve", kind: "gate", is_output: true }),
      ],
      edges: [
        { from: "compose", to: "approve", kind: "wire", label: "reviews: string" },
        { from: "approve", to: "compose", kind: "rejected", label: "rejected" },
      ],
    };
    expect(executionOrder(graph).map((n) => n.id)).toEqual(["compose", "approve"]);
  });

  it("puts a node no source reaches at the end, in its own original order", () => {
    const graph: WorkflowGraph = {
      name: "cycle",
      version: 1,
      nodes: [
        node({ id: "a", kind: "run" }),
        node({ id: "b", kind: "run" }),
        node({ id: "c", kind: "run" }),
      ],
      edges: [
        { from: "b", to: "c", kind: "wire", label: "" },
        { from: "c", to: "b", kind: "wire", label: "" },
      ],
    };
    expect(executionOrder(graph).map((n) => n.id)).toEqual(["a", "b", "c"]);
  });
});

describe("levels", () => {
  it("groups digests and references at the same depth, after undigested and before report", () => {
    const result = levels(RESEARCH_GRAPH).map((group) => group.map((n) => n.id));
    expect(result).toEqual([["undigested"], ["digests", "references"], ["report"]]);
  });

  it("excludes params from every level, even though a param sits at depth 0", () => {
    const ids = levels(RESEARCH_GRAPH).flatMap((group) => group.map((n) => n.id));
    expect(ids).not.toContain("collection");
  });
});

describe("shapeCounts", () => {
  it("counts gate as a person gate, judge and orchestrator-gate as agent gates, param skipped", () => {
    const graph: WorkflowGraph = {
      name: "shapes",
      version: 1,
      nodes: [
        node({ id: "p", kind: "param" }),
        node({ id: "a", kind: "run" }),
        node({ id: "b", kind: "map" }),
        node({ id: "g", kind: "gate" }),
        node({ id: "j", kind: "judge" }),
        node({ id: "o", kind: "orchestrator-gate" }),
      ],
      edges: [],
    };
    expect(shapeCounts(graph)).toEqual({ steps: 2, person_gates: 1, agent_gates: 2 });
  });
});

describe("wiresInto", () => {
  it("reads a parameter source, stripping the param: prefix and naming fromKind parameter", () => {
    const wires = wiresInto(RESEARCH_GRAPH, "undigested");
    expect(wires).toEqual([
      { arg: "collection", from: "collection", fromKind: "parameter", each: false },
    ]);
  });

  it("reads a node source on a plain wire, fromKind node, each false", () => {
    const wires = wiresInto(RESEARCH_GRAPH, "report");
    expect(wires).toEqual([
      { arg: "digests", from: "digests", fromKind: "node", each: false },
      { arg: "references", from: "references", fromKind: "node", each: false },
    ]);
  });

  it("strips the leading 'each ' on a map edge's label before reading the arg, each true", () => {
    const wires = wiresInto(RESEARCH_GRAPH, "digests");
    expect(wires).toEqual([{ arg: "source", from: "undigested", fromKind: "node", each: true }]);
  });

  it("never returns a rejected edge", () => {
    const graph: WorkflowGraph = {
      name: "gated",
      version: 1,
      nodes: [node({ id: "compose", kind: "run" }), node({ id: "approve", kind: "gate" })],
      edges: [
        { from: "compose", to: "approve", kind: "wire", label: "reviews: string" },
        { from: "approve", to: "compose", kind: "rejected", label: "rejected" },
      ],
    };
    expect(wiresInto(graph, "compose")).toEqual([]);
  });
});

describe("rejectedTo and passesTo", () => {
  const GATE_GRAPH: WorkflowGraph = {
    name: "gated",
    version: 1,
    nodes: [
      node({ id: "compose", kind: "run" }),
      node({ id: "approve", kind: "gate" }),
      node({ id: "publish", kind: "run", is_output: true }),
    ],
    edges: [
      { from: "compose", to: "approve", kind: "wire", label: "reviews: string" },
      { from: "approve", to: "compose", kind: "rejected", label: "rejected" },
      { from: "approve", to: "publish", kind: "wire", label: "reviews: string" },
    ],
  };

  it("reads the rejected edge's own target", () => {
    expect(rejectedTo(GATE_GRAPH, "approve")).toBe("compose");
  });

  it("returns null when a gate carries no rejected edge", () => {
    expect(rejectedTo(GATE_GRAPH, "compose")).toBeNull();
  });

  it("reads a gate's outgoing wire edges as what it passes on", () => {
    expect(passesTo(GATE_GRAPH, "approve")).toEqual(["publish"]);
  });

  it("returns an empty list when the gate is the run's own output, nothing downstream", () => {
    const graph: WorkflowGraph = {
      ...GATE_GRAPH,
      nodes: [
        node({ id: "compose", kind: "run" }),
        node({ id: "approve", kind: "gate", is_output: true }),
      ],
      edges: [
        { from: "compose", to: "approve", kind: "wire", label: "reviews: string" },
        { from: "approve", to: "compose", kind: "rejected", label: "rejected" },
      ],
    };
    expect(passesTo(graph, "approve")).toEqual([]);
  });
});

describe("resolveRef", () => {
  const ROOT = {
    $defs: {
      SalesData: { type: "object", title: "SalesData", properties: { total: { type: "number" } } },
    },
  };

  it("resolves a $ref against the root schema's own $defs", () => {
    expect(resolveRef({ $ref: "#/$defs/SalesData" }, ROOT)).toEqual(ROOT.$defs.SalesData);
  });

  it("returns the schema itself when it carries no $ref", () => {
    const schema = { type: "string" };
    expect(resolveRef(schema, ROOT)).toBe(schema);
  });

  it("falls back to the schema itself when the ref cannot be resolved", () => {
    const schema = { $ref: "#/$defs/Missing" };
    expect(resolveRef(schema, ROOT)).toBe(schema);
  });
});

describe("typeLabel", () => {
  it("reads a schema's own title when it carries one", () => {
    expect(typeLabel({ type: "object", title: "DigestResult" })).toBe("DigestResult");
  });

  it("reads list of X for an array, recursing into its items", () => {
    expect(typeLabel({ type: "array", items: { type: "object", title: "Undigested" } })).toBe(
      "list of Undigested",
    );
  });

  it("reads a bare JSON type when the schema carries no title", () => {
    expect(typeLabel({ type: "string", minLength: 1 })).toBe("string");
  });

  it("reads anyOf of a shape and null as the shape alone", () => {
    expect(typeLabel({ anyOf: [{ type: "string" }, { type: "null" }] })).toBe("string");
  });

  it("resolves a $ref before reading the title", () => {
    const root = { $defs: { SalesData: { type: "object", title: "SalesData" } } };
    expect(typeLabel({ $ref: "#/$defs/SalesData" }, root)).toBe("SalesData");
  });

  it("falls back to object for a shape it does not otherwise recognize", () => {
    expect(typeLabel({})).toBe("object");
  });

  it("ignores the title pydantic puts on a primitive field, reading its type instead", () => {
    expect(typeLabel({ title: "Item Type", type: "string", enum: ["video", "paper"] })).toBe(
      "string",
    );
  });

  it("resolves a $ref against $defs the items schema itself carries, as a wrapped item does", () => {
    const wrapped = {
      type: "array",
      items: {
        $defs: { Reference: { type: "object", title: "Reference" } },
        type: "array",
        items: { $ref: "#/$defs/Reference" },
      },
    };
    expect(typeLabel(wrapped)).toBe("list of list of Reference");
  });
});

describe("unwrapNullable", () => {
  it("returns the one shape inside anyOf with null, and any other schema as it is", () => {
    const shape = { enum: ["a", "b"], type: "string" };
    expect(unwrapNullable({ anyOf: [shape, { type: "null" }], default: null })).toBe(shape);
    const plain = { type: "string" };
    expect(unwrapNullable(plain)).toBe(plain);
    const twoShapes = { anyOf: [{ type: "string" }, { type: "integer" }] };
    expect(unwrapNullable(twoShapes)).toBe(twoShapes);
  });
});

describe("rootFor", () => {
  it("prefers a child that carries its own $defs, else the root it sits under", () => {
    const root = { $defs: {} };
    const child = { $defs: { X: {} } };
    expect(rootFor(child, root)).toBe(child);
    expect(rootFor({ type: "string" }, root)).toBe(root);
  });
});

describe("isJsonRecord", () => {
  it("accepts a plain object, rejects an array, null, and a primitive", () => {
    expect(isJsonRecord({})).toBe(true);
    expect(isJsonRecord([])).toBe(false);
    expect(isJsonRecord(null)).toBe(false);
    expect(isJsonRecord("x")).toBe(false);
  });
});
