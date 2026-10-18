"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { type ReactElement, useState } from "react";

// A recursive, collapsible view of arbitrary JSON (a tool call's args, a tool
// return's content): objects and arrays are default-expanded to a fixed depth
// then collapsible from there, primitives render inline, and a string longer
// than the threshold is truncated with a button the caller wires to a viewer.

export const LONG_STRING_THRESHOLD = 240;

interface JsonTreeProps {
  value: unknown;
  name?: string;
  depth?: number;
  defaultExpandDepth?: number;
  onView: (text: string) => void;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function primitiveLabel(value: unknown): string {
  if (value === undefined) {
    return "undefined";
  }
  return JSON.stringify(value);
}

function NameLabel({ name }: { name?: string }): ReactElement | null {
  if (name === undefined) {
    return null;
  }
  return <span className="text-label-3">{name}</span>;
}

export function JsonTree({
  value,
  name,
  depth = 0,
  defaultExpandDepth = 2,
  onView,
}: JsonTreeProps): ReactElement {
  if (typeof value === "string" && value.length > LONG_STRING_THRESHOLD) {
    return (
      <div className="flex flex-wrap items-baseline gap-1.5">
        <NameLabel name={name} />
        <span className="font-mono text-label-2 text-xs">
          {JSON.stringify(value.slice(0, LONG_STRING_THRESHOLD))}
        </span>
        <button
          type="button"
          onClick={() => onView(value)}
          className="text-accent text-xs underline"
        >
          View
        </button>
      </div>
    );
  }

  if (Array.isArray(value) || isPlainObject(value)) {
    return (
      <JsonBranch
        value={value}
        name={name}
        depth={depth}
        defaultExpandDepth={defaultExpandDepth}
        onView={onView}
      />
    );
  }

  return (
    <div className="flex items-baseline gap-1.5">
      <NameLabel name={name} />
      <span className="font-mono text-label-2 text-xs">{primitiveLabel(value)}</span>
    </div>
  );
}

function JsonBranch({
  value,
  name,
  depth,
  defaultExpandDepth,
  onView,
}: {
  value: unknown[] | Record<string, unknown>;
  name?: string;
  depth: number;
  defaultExpandDepth: number;
  onView: (text: string) => void;
}): ReactElement {
  const [expanded, setExpanded] = useState(depth < defaultExpandDepth);
  const isArray = Array.isArray(value);
  const entries: [string, unknown][] = isArray
    ? value.map((item, index) => [String(index), item])
    : Object.entries(value);
  const summary = isArray ? `Array(${entries.length})` : `Object(${entries.length})`;

  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
        className="flex items-center gap-1 text-left text-label-2 text-xs hover:text-label-1"
      >
        {expanded ? (
          <ChevronDown size={12} strokeWidth={1.75} aria-hidden />
        ) : (
          <ChevronRight size={12} strokeWidth={1.75} aria-hidden />
        )}
        <NameLabel name={name} />
        <span className="text-label-3">{summary}</span>
      </button>
      {expanded ? (
        <div className="mt-1 flex flex-col gap-1 border-line border-l-2 pl-3">
          {entries.map(([entryName, entryValue]) => (
            <JsonTree
              key={entryName}
              value={entryValue}
              name={entryName}
              depth={depth + 1}
              defaultExpandDepth={defaultExpandDepth}
              onView={onView}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
