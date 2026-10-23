"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import dynamic from "next/dynamic";
import { type KeyboardEvent, useMemo, useState } from "react";

import { type GraphFilters as GraphFilterValues, knowledgeBaseApi } from "@/api/knowledge-base";
import type { GraphNode, GraphResult } from "@/api/types";
import { nodeColor } from "@/lib/graph-model";
import { cn } from "@/shared/lib/cn";
import { Card } from "@/shared/ui/card";
import { Input } from "@/shared/ui/input";

import { GraphDetailPanel, type GraphSelection } from "./graph-detail-panel";
import { GraphFilters } from "./graph-filters";

// Loaded client-side ONLY. sigma reads `WebGL2RenderingContext` at module
// evaluation, which does not exist in Node, a plain import made the server
// render of this page throw.
const GraphCanvas = dynamic(() => import("./graph-canvas").then((module) => module.GraphCanvas), {
  ssr: false,
  loading: () => (
    <p className="absolute inset-0 flex items-center justify-center text-body text-label-3">
      Loading the graph.
    </p>
  ),
});

// One knowledge base's link graph, rendered as the Knowledge-bases detail page's Graph tab.
// One GET /api/graph?knowledge_base={name} fetch, re-fetched client-side when a filter
// changes; sigma.js draws it, this component is the reading surface around it: how
// much is on screen, a key to the colours, a way to FIND one page among hundreds,
// and a detail panel the reader can walk the graph from.

const EMPTY_GRAPH: GraphResult = { nodes: [], edges: [] };
const MAX_SUGGESTIONS = 8;

export interface KnowledgeBaseGraphViewProps {
  knowledgeBaseName: string;
}

export function KnowledgeBaseGraphView({ knowledgeBaseName }: KnowledgeBaseGraphViewProps) {
  const [filters, setFilters] = useState<GraphFilterValues>({});
  const [selection, setSelection] = useState<GraphSelection>(null);
  const [term, setTerm] = useState("");
  const [cursor, setCursor] = useState(-1);
  const [dismissed, setDismissed] = useState(false);

  const { data, isPending, isError, error } = useQuery({
    // knowledgeBaseName + filters are both part of the key, so switching either
    // re-fetches with the new query params; keepPreviousData avoids a loading
    // flash on refetch.
    queryKey: ["graph", knowledgeBaseName, filters],
    queryFn: () => knowledgeBaseApi.graph({ ...filters, knowledge_base: knowledgeBaseName }),
    placeholderData: keepPreviousData,
  });

  const graph = data ?? EMPTY_GRAPH;

  // The colour key lists only the types actually on screen, a legend naming
  // page types the knowledge base does not contain teaches the reader something false.
  const typesPresent = useMemo(
    () => [...new Set(graph.nodes.map((node) => node.type))].sort(),
    [graph.nodes],
  );

  // Pages nothing links to. Stated rather than left to be inferred from a
  // scatter of loose dots: it is the knowledge base's own health signal, and the
  // reader deserves the number.
  const unlinked = useMemo(() => {
    const touched = new Set(graph.edges.flatMap((edge) => [edge.src_id, edge.dst_id]));
    return graph.nodes.filter((node) => !touched.has(node.id)).length;
  }, [graph]);

  const suggestions = useMemo(() => {
    const needle = term.trim().toLowerCase();
    if (!needle) return [];
    return graph.nodes
      .filter((node) => node.title.toLowerCase().includes(needle))
      .slice(0, MAX_SUGGESTIONS);
  }, [graph.nodes, term]);

  function selectById(id: string) {
    const node = graph.nodes.find((candidate) => candidate.id === id);
    if (node) setSelection({ node });
  }

  const selectedId = selection && "node" in selection ? selection.node.id : null;
  const open = suggestions.length > 0 && !dismissed;
  // Nothing is active until the reader arrows into the list, an option
  // pre-selected on the first keystroke is one an unlucky Enter commits to.
  const active = open && cursor >= 0 ? suggestions[cursor] : undefined;

  function pick(node: GraphNode) {
    setSelection({ node });
    setTerm("");
    setCursor(-1);
  }

  function onSearchKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      setDismissed(true);
      return;
    }
    if (!open) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setCursor((current) => (current + 1) % suggestions.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setCursor((current) => (current <= 0 ? suggestions.length : current) - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      const chosen = active ?? suggestions[0];
      if (chosen) pick(chosen);
    }
  }

  return (
    <div className="flex h-[560px] flex-col overflow-hidden rounded-lg border border-line bg-surface">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-line border-b px-3 py-2">
        <p className="text-caption text-label-3 tabular-nums">
          {graph.nodes.length} pages, {graph.edges.length} links
          {unlinked > 0 ? `, ${unlinked} unlinked` : ""}
        </p>

        {/* The ARIA combobox pattern: focus never leaves the input, which points
            at the active option through `aria-activedescendant`. Arrow keys move,
            Enter picks, Escape dismisses, a search box that only answers to a
            mouse is half a search box. */}
        <div className="relative">
          <Search
            aria-hidden
            size={14}
            strokeWidth={1.75}
            className="-translate-y-1/2 pointer-events-none absolute top-1/2 left-2.5 text-label-3"
          />
          <Input
            role="combobox"
            aria-label="Find a page"
            aria-expanded={open}
            aria-controls="graph-search-results"
            aria-activedescendant={active ? `graph-option-${active.id}` : undefined}
            autoComplete="off"
            value={term}
            onChange={(event) => {
              setTerm(event.target.value);
              setCursor(-1);
              setDismissed(false);
            }}
            onKeyDown={onSearchKeyDown}
            placeholder="Find a page"
            className="w-56 pl-7"
          />
          {open ? (
            <Card
              id="graph-search-results"
              // biome-ignore lint/a11y/useSemanticElements: a <select> cannot render a filtered, typed, colour-keyed autocomplete; this is the W3C APG combobox pattern, where the listbox is a div and the input keeps focus
              role="listbox"
              tabIndex={-1}
              aria-label="Matching pages"
              className="absolute top-full left-0 z-20 mt-1 max-h-64 w-72 overflow-y-auto py-1 shadow-lg"
            >
              {suggestions.map((node, index) => (
                <div
                  key={node.id}
                  id={`graph-option-${node.id}`}
                  // biome-ignore lint/a11y/useSemanticElements: <option> cannot hold the colour swatch this row shows, and the APG pattern keeps options unfocusable, addressed by aria-activedescendant
                  role="option"
                  tabIndex={-1}
                  aria-selected={index === cursor}
                  onClick={() => pick(node)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") pick(node);
                  }}
                  className={cn(
                    "flex cursor-pointer items-center gap-2 px-2.5 py-1 text-caption text-label-2",
                    index === cursor && "bg-well",
                  )}
                >
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ backgroundColor: nodeColor(node.type) }}
                  />
                  <span className="truncate">{node.title}</span>
                </div>
              ))}
            </Card>
          ) : null}
        </div>

        <GraphFilters
          graph={graph}
          value={filters}
          onChange={(next) => {
            setFilters(next);
            setSelection(null);
          }}
          className="flex items-center gap-3"
        />

        <ul
          aria-label="Colour key"
          className="ml-auto flex flex-wrap items-center gap-x-2.5 gap-y-1"
        >
          {typesPresent.map((type) => (
            <li key={type} className="flex items-center gap-1 text-label-2 text-micro">
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: nodeColor(type) }} />
              {type}
            </li>
          ))}
        </ul>
      </div>

      {isError ? (
        <p role="alert" className="bg-danger-soft px-3 py-1.5 text-caption text-danger-ink">
          Failed to load graph: {error instanceof Error ? error.message : "unknown error"}
        </p>
      ) : null}

      <div className="flex min-h-0 flex-1">
        <div className="relative min-h-0 min-w-0 flex-1">
          {isPending ? (
            <p className="absolute inset-0 flex items-center justify-center text-body text-label-3">
              Loading the graph.
            </p>
          ) : (
            <GraphCanvas
              graph={graph}
              selectedId={selectedId}
              onSelect={setSelection}
              className="h-full w-full"
            />
          )}
        </div>
        <GraphDetailPanel
          selection={selection}
          graph={graph}
          onSelectNode={selectById}
          className={cn("w-64 shrink-0 overflow-y-auto border-line border-l bg-surface px-3 py-3")}
        />
      </div>
    </div>
  );
}
