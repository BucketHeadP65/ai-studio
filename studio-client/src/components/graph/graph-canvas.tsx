"use client";

import { SigmaContainer, useRegisterEvents, useSigma } from "@react-sigma/core";
import "@react-sigma/core/lib/style.css";
import { type RefObject, useEffect, useMemo, useRef, useState } from "react";
import { extremityArrow } from "sigma/rendering";
import { DEFAULT_STYLES, type SigmaOptions } from "sigma/types";

import type { GraphEdge, GraphNode, GraphResult } from "@/api/types";
import { graphSignature, toGraphology } from "@/lib/graph-model";

import { drawEdgeLabels } from "./edge-labels";

// The knowledge base graph's canvas, on sigma.js v4 + graphology, the stack built for
// exactly this (WebGL network rendering, Gephi lineage) instead of the manual
// cytoscape wrapper it replaces. graphology holds the model, ForceAtlas2 lays
// it out ONCE (deterministically seeded, so the map doesn't reshuffle on every
// visit), sigma draws and handles hover/click. The pure GraphResult mapping
// lives in @/lib/graph-model, where jsdom can test it without WebGL.

const MUTED = "#e8edf3";

export interface GraphCanvasProps {
  graph: GraphResult;
  // Tap on a node or edge yields the DOMAIN object, the rest of the app never
  // sees sigma internals.
  onSelect?: (selected: { node: GraphNode } | { edge: GraphEdge }) => void;
  // The node the page considers selected (found by search, or reached by
  // walking the detail panel). The camera flies to it and it is ringed, a
  // selection the reader cannot SEE is not a selection.
  selectedId?: string | null;
  className?: string;
}

// What the reducers read on every refresh: the hovered node and the page's selection.
interface Focus {
  hovered: string | null;
  selectedId: string | null;
}

// Sizes stay in screen pixels (sigma 4 defaults to layout units), every edge
// draws its arrow head, and the node label size and colour ride the styles.
// sigma's own edge labels stay off: UprightEdgeLabels draws them.
function sigmaOptions(focus: RefObject<Focus>): SigmaOptions {
  return {
    settings: {
      renderEdgeLabels: false,
      enableEdgeEvents: true,
      labelDensity: 1,
      labelGridCellSize: 90,
      labelRenderedSizeThreshold: 5,
      minCameraRatio: 0.05,
      maxCameraRatio: 8,
      allowInvalidContainer: true,
      itemSizesReference: "screen",
    },
    primitives: { edges: { extremities: [extremityArrow()] } },
    styles: {
      nodes: [DEFAULT_STYLES.nodes, { labelSize: 11, labelColor: "#334155" }],
      edges: [DEFAULT_STYLES.edges, { head: "arrow" }],
    },
    // Focus, in one reducer pair. Hovering a node keeps it and its neighbourhood
    // in full colour and fades the rest, the standard way to read a hairball one
    // ego-network at a time, and the selected node is ringed and always labelled
    // so the reader can find what they searched for.
    nodeReducer: (node, _data, _attributes, _state, _graphState, graph) => {
      const { hovered, selectedId } = focus.current;
      const center = hovered ?? selectedId;
      const ringed =
        node === selectedId
          ? {
              backdropVisibility: "visible" as const,
              labelVisibility: "visible" as const,
              depth: "topNodes",
            }
          : {};
      if (center === null) return ringed;
      if (node === center || graph.areNeighbors(node, center))
        return { depth: "topNodes", ...ringed };
      return { ...ringed, color: MUTED, label: "" };
    },
    edgeReducer: (edge, _data, _attributes, _state, _graphState, graph) => {
      const { hovered, selectedId } = focus.current;
      const center = hovered ?? selectedId;
      if (center === null) return {};
      if (graph.source(edge) === center || graph.target(edge) === center) {
        return { color: "#94a3b8", depth: "topEdges" };
      }
      return { color: MUTED, label: "" };
    },
  };
}

function GraphSync({
  graph,
  onSelect,
  selectedId,
  focus,
}: Pick<GraphCanvasProps, "graph" | "onSelect" | "selectedId"> & { focus: RefObject<Focus> }) {
  const sigma = useSigma();
  const registerEvents = useRegisterEvents();
  const [hovered, setHovered] = useState<string | null>(null);

  const signature = useMemo(() => graphSignature(graph), [graph]);

  // Load + lay out when the CONTENT changes; a refetch that resolves to the
  // identical graph keeps its positions and camera.
  // biome-ignore lint/correctness/useExhaustiveDependencies: signature IS the graph identity
  useEffect(() => {
    sigma.setGraph(toGraphology(graph));
    void sigma.getCamera().reset({ duration: 250 });
    sigma.refresh();
  }, [signature, sigma]);

  useEffect(() => {
    registerEvents({
      clickNode: ({ node }) => {
        const domain = graph.nodes.find((n) => n.id === node);
        if (domain) onSelect?.({ node: domain });
      },
      clickEdge: ({ edge }) => {
        const g = sigma.getGraph();
        const [src, dst, rel] = [g.source(edge), g.target(edge), g.getEdgeAttribute(edge, "label")];
        const domain = graph.edges.find(
          (e) => e.src_id === src && e.dst_id === dst && e.rel === rel,
        );
        if (domain) onSelect?.({ edge: domain });
      },
      enterNode: ({ node }) => setHovered(node),
      leaveNode: () => setHovered(null),
    });
  }, [registerEvents, graph, onSelect, sigma]);

  // Fly to whatever the page selected. Only when the id changes, so a hover
  // repaint never yanks the camera.
  useEffect(() => {
    if (!selectedId) return;
    const g = sigma.getGraph();
    if (!g.hasNode(selectedId)) return;
    void sigma.getCamera().animate(
      {
        x: sigma.getNodeDisplayData(selectedId)?.x ?? 0.5,
        y: sigma.getNodeDisplayData(selectedId)?.y ?? 0.5,
        // Close enough to read the neighbourhood, wide enough to keep some
        // context around it, landing at full zoom on one dot is disorienting.
        ratio: 0.5,
      },
      { duration: 400 },
    );
  }, [selectedId, sigma]);

  // The reducers read the focus at render time, so a change only asks for a refresh.
  useEffect(() => {
    focus.current = { hovered, selectedId: selectedId ?? null };
    sigma.refresh();
  }, [hovered, selectedId, focus, sigma]);

  return null;
}

const EDGE_LABEL_LAYER = "edge-labels";

// The edge labels, upright in place of sigma 4's own (sigma.js issue 1544, see
// edge-labels.ts). A 2D canvas under the WebGL stage, redrawn after every render,
// so nodes stay on top of the labels as they did on sigma 3.
function UprightEdgeLabels({ focus }: { focus: RefObject<Focus> }) {
  const sigma = useSigma();

  useEffect(() => {
    const canvas = sigma.createCanvas(EDGE_LABEL_LAYER, { beforeLayer: "stage" });
    const context = canvas.getContext("2d");
    const draw = () => {
      if (context === null) return;
      const { hovered, selectedId } = focus.current;
      drawEdgeLabels(sigma, context, hovered ?? selectedId);
    };
    sigma.on("afterRender", draw);
    sigma.scheduleRender();
    return () => {
      sigma.off("afterRender", draw);
      // A killed renderer has already emptied its container, layer included.
      if (canvas.isConnected) sigma.killLayer(EDGE_LABEL_LAYER);
    };
  }, [sigma, focus]);

  return null;
}

export function GraphCanvas({ graph, onSelect, selectedId, className }: GraphCanvasProps) {
  const focus = useRef<Focus>({ hovered: null, selectedId: selectedId ?? null });
  // Built once: a new options object would make the container build a new renderer.
  const options = useMemo(() => sigmaOptions(focus), []);
  return (
    <SigmaContainer
      className={className}
      style={{ width: "100%", height: "100%", background: "#fff" }}
      options={options}
      data-testid="graph-canvas"
    >
      <GraphSync graph={graph} onSelect={onSelect} selectedId={selectedId} focus={focus} />
      <UprightEdgeLabels focus={focus} />
    </SigmaContainer>
  );
}
