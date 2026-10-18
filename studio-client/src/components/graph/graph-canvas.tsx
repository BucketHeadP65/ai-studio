"use client";

import { SigmaContainer, useRegisterEvents, useSetSettings, useSigma } from "@react-sigma/core";
import "@react-sigma/core/lib/style.css";
import { useEffect, useMemo, useState } from "react";

import type { GraphEdge, GraphNode, GraphResult } from "@/api/types";
import { graphSignature, toGraphology } from "@/lib/graph-model";

// The knowledge base graph's canvas, on sigma.js v3 + graphology, the stack built for
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

const SIGMA_SETTINGS = {
  renderEdgeLabels: true,
  enableEdgeEvents: true,
  labelSize: 11,
  labelColor: { color: "#334155" },
  edgeLabelSize: 9,
  edgeLabelColor: { color: "#94a3b8" },
  labelDensity: 1,
  labelGridCellSize: 90,
  labelRenderedSizeThreshold: 5,
  minCameraRatio: 0.05,
  maxCameraRatio: 8,
  allowInvalidContainer: true,
} as const;

function GraphSync({
  graph,
  onSelect,
  selectedId,
}: Pick<GraphCanvasProps, "graph" | "onSelect" | "selectedId">) {
  const sigma = useSigma();
  const registerEvents = useRegisterEvents();
  const setSettings = useSetSettings();
  const [hovered, setHovered] = useState<string | null>(null);

  const signature = useMemo(() => graphSignature(graph), [graph]);

  // Load + lay out when the CONTENT changes; a refetch that resolves to the
  // identical graph keeps its positions and camera.
  // biome-ignore lint/correctness/useExhaustiveDependencies: signature IS the graph identity
  useEffect(() => {
    sigma.setGraph(toGraphology(graph));
    sigma.getCamera().animatedReset({ duration: 250 });
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
    sigma.getCamera().animate(
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

  // Focus, in one reducer pair. Hovering a node keeps it and its neighbourhood
  // in full colour and fades the rest, the standard way to read a hairball one
  // ego-network at a time, and the selected node is ringed and always labelled
  // so the reader can find what they searched for.
  useEffect(() => {
    const focus = hovered ?? selectedId ?? null;
    setSettings({
      nodeReducer: (node, data) => {
        const ringed =
          node === selectedId ? { highlighted: true, forceLabel: true, zIndex: 2 } : { zIndex: 0 };
        if (focus === null) return { ...data, ...ringed };
        const g = sigma.getGraph();
        if (node === focus || g.areNeighbors(node, focus)) {
          return { ...data, ...ringed, zIndex: Math.max(1, ringed.zIndex ?? 0) };
        }
        return { ...data, ...ringed, color: MUTED, label: "" };
      },
      edgeReducer: (edge, data) => {
        if (focus === null) return data;
        const g = sigma.getGraph();
        if (g.source(edge) === focus || g.target(edge) === focus) {
          return { ...data, color: "#94a3b8", zIndex: 1 };
        }
        return { ...data, color: MUTED, label: "", zIndex: 0 };
      },
    });
  }, [hovered, selectedId, setSettings, sigma]);

  return null;
}

export function GraphCanvas({ graph, onSelect, selectedId, className }: GraphCanvasProps) {
  return (
    <SigmaContainer
      className={className}
      style={{ width: "100%", height: "100%", background: "#fff" }}
      settings={SIGMA_SETTINGS}
      data-testid="graph-canvas"
    >
      <GraphSync graph={graph} onSelect={onSelect} selectedId={selectedId} />
    </SigmaContainer>
  );
}
