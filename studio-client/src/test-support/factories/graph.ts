import { faker } from "@faker-js/faker";
import { Factory } from "fishery";

import type { GraphEdge, GraphNode, GraphResult } from "@/api/types";

export const graphNodeFactory = Factory.define<GraphNode>(({ sequence }) => ({
  id: `paper:${faker.lorem.slug(2)}-${sequence}`,
  type: "paper",
  title: faker.lorem.sentence({ min: 3, max: 7 }),
  status: "synthesized",
  superseded: false,
}));

export const graphEdgeFactory = Factory.define<GraphEdge>(() => ({
  src_id: `paper:${faker.lorem.slug(2)}`,
  dst_id: `paper:${faker.lorem.slug(2)}`,
  rel: "builds_on",
  provenance_claim: null,
}));

// A small connected graph: two nodes plus one edge between them. Override at
// call-time for empty / larger / filtered shapes.
export const graphResultFactory = Factory.define<GraphResult>(() => {
  const [a, b] = graphNodeFactory.buildList(2);
  return {
    nodes: [a, b],
    edges: [graphEdgeFactory.build({ src_id: a.id, dst_id: b.id })],
  };
});
