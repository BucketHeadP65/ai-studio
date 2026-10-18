"use client";

import type { GraphFilters as GraphFilterValues } from "@/api/knowledge-base";
import type { GraphResult } from "@/api/types";

// Two controlled selects, `page_type` and `rel`, whose options are DERIVED
// from the currently loaded graph data (distinct node types / edge rels), each
// with an "All" default. Changes propagate up via DI callbacks; the parent owns
// the filter state and re-fetches (filtering is server-side, see knowledge-base-graph-view).
//
// Deriving options from the loaded graph (rather than a hardcoded enum) keeps
// the dropdowns honest to what the knowledge base actually contains and means a new
// page kind / relation appears automatically once it's in the data.

// Distinct node `type` values, sorted, for the page_type select.
export function pageTypeOptions(graph: GraphResult): string[] {
  return [...new Set(graph.nodes.map((n) => n.type))].sort();
}

// Distinct edge `rel` values, sorted, for the rel select.
export function relOptions(graph: GraphResult): string[] {
  return [...new Set(graph.edges.map((e) => e.rel))].sort();
}

// The select's "no filter" sentinel. Empty string maps to `undefined` in the
// GraphFilters object so the query string omits the param entirely.
const ALL = "";

export interface GraphFiltersProps {
  graph: GraphResult;
  value: GraphFilterValues;
  onChange: (next: GraphFilterValues) => void;
  className?: string;
}

export function GraphFilters({ graph, value, onChange, className }: GraphFiltersProps) {
  const pageTypes = pageTypeOptions(graph);
  const rels = relOptions(graph);

  return (
    <fieldset className={className}>
      <legend className="sr-only">Graph filters</legend>
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-label-2">Page type</span>
        <select
          aria-label="Page type"
          value={value.page_type ?? ALL}
          onChange={(e) =>
            onChange({ ...value, page_type: e.target.value === ALL ? undefined : e.target.value })
          }
          className="rounded border border-line-strong px-2 py-1"
        >
          <option value={ALL}>All</option>
          {pageTypes.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-sm">
        <span className="text-label-2">Relation</span>
        <select
          aria-label="Relation"
          value={value.rel ?? ALL}
          onChange={(e) =>
            onChange({ ...value, rel: e.target.value === ALL ? undefined : e.target.value })
          }
          className="rounded border border-line-strong px-2 py-1"
        >
          <option value={ALL}>All</option>
          {rels.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </label>
    </fieldset>
  );
}
