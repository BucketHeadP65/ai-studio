"use client";

import { formatRelative } from "client-core/lib/format-relative";
import { Button } from "client-core/ui/button";
import { Card } from "client-core/ui/card";
import { EmptyState } from "client-core/ui/empty-state";
import { Database } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactElement } from "react";

import type { KnowledgeBaseListEntry } from "@/api/types";

interface Props {
  entries: KnowledgeBaseListEntry[];
  loading: boolean;
  onViewSchema: (name: string) => void;
  onOpenTerminal: (path: string) => void;
}

const GRID = "grid grid-cols-[1fr_130px_150px_210px] items-center gap-3";
const COLUMN_HEAD = "font-semibold text-label-3 text-micro uppercase tracking-caps";

function CheckIcon(): ReactElement {
  return (
    <svg
      width={14}
      height={14}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.3}
      aria-hidden="true"
    >
      <path d="M3.2 8.4l3.2 3.2 6.4-7" />
    </svg>
  );
}

function WarningIcon(): ReactElement {
  return (
    <svg
      width={14}
      height={14}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.9}
      aria-hidden="true"
    >
      <path d="M8 1.8l6.4 11.4H1.6z" />
      <path d="M8 6.4v3M8 11.4h.01" />
    </svg>
  );
}

function DashedDotIcon(): ReactElement {
  return (
    <svg
      width={14}
      height={14}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeDasharray="2.2 2.2"
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r="6" />
    </svg>
  );
}

// A base's index state, one of four the registry ∪ indexed-DB union can carry: broken (its
// registered directory moved or was deleted), indexed (readable, timed off its
// own last index), registered but never indexed, or not registered at all. Each state is
// an icon plus its own sentence, never a color alone.
function IndexState({ entry }: { entry: KnowledgeBaseListEntry }): ReactElement {
  if (entry.broken) {
    return (
      <span className="flex items-center gap-1.5 text-body text-warn-ink">
        <WarningIcon />
        Broken.
      </span>
    );
  }
  if (entry.indexed) {
    return (
      <span className="flex items-center gap-1.5 text-body text-label-2">
        <CheckIcon />
        {entry.last_indexed ? `Indexed ${formatRelative(entry.last_indexed)}.` : "Indexed."}
      </span>
    );
  }
  if (entry.registered) {
    return (
      <span className="flex items-center gap-1.5 text-body text-label-3">
        <DashedDotIcon />
        Not indexed yet.
      </span>
    );
  }
  return <span className="text-body text-label-3">Not registered.</span>;
}

// The registry ∪ indexed-DB union, one row per name. `registered` and `indexed` are
// independent (the backend's own framing) so all four index states render honestly: a name
// can be registered without ever indexing, indexed without being registered, both, or —
// simply by not appearing, neither. `broken` marks a registry row whose directory moved or
// was deleted; it's surfaced (name, raw path) rather than hidden, with no per-row actions
// since those would just fail the same way.
export function KnowledgeBasesList({ entries, loading, onViewSchema, onOpenTerminal }: Props) {
  const router = useRouter();
  const ordered = [...entries].sort((a, b) => a.name.localeCompare(b.name));

  if (loading) {
    return <p className="text-body text-label-3">Loading knowledge bases...</p>;
  }
  if (ordered.length === 0) {
    return (
      <EmptyState
        icon={<Database size={20} strokeWidth={1.5} />}
        title="Nothing here yet."
        hint="A knowledge base is an indexed folder of markdown notes the agent can read."
      />
    );
  }

  return (
    <Card className="overflow-hidden rounded-xl shadow-sm">
      <div className={`${GRID} border-line border-b bg-well px-5 py-3`}>
        <span className={COLUMN_HEAD}>Base</span>
        <span className={COLUMN_HEAD}>Documents</span>
        <span className={COLUMN_HEAD}>Types</span>
        <span className={COLUMN_HEAD}>Index state</span>
      </div>
      <ul>
        {ordered.map((entry, index) => {
          const href = `/knowledge-bases/${encodeURIComponent(entry.name)}`;
          const path = entry.path;
          const showActions = entry.registered && !entry.broken;
          const last = index === ordered.length - 1;
          return (
            // The whole row opens the base's own page. A click on any of the row's own controls
            // (the name's own link, View schema, Terminal) keeps its own meaning instead.
            // biome-ignore lint/a11y/useKeyWithClickEvents: the li is not focusable, so no key event can reach it, the keyboard path is the base name's own link.
            <li
              key={entry.name}
              onClick={(event) => {
                if (
                  event.target instanceof Element &&
                  event.target.closest("button, input, a, textarea")
                ) {
                  return;
                }
                router.push(href);
              }}
              className={`${GRID} cursor-pointer px-5 py-4 transition-colors hover:bg-well ${last ? "" : "border-ground border-b"}`}
            >
              <div className="min-w-0">
                <Link
                  href={href}
                  className="block truncate font-semibold text-body text-label-1 hover:text-accent"
                >
                  {entry.name}
                </Link>
                <p className="truncate font-mono text-label-3 text-micro">{path ?? ""}</p>
              </div>
              <span className="font-mono text-body text-label-2">
                {entry.pages === null ? "" : entry.pages}
              </span>
              <span className="font-mono text-body text-label-2">{entry.page_types.length}</span>
              <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5">
                <IndexState entry={entry} />
                {showActions ? (
                  <div className="flex items-center gap-1.5">
                    <Button
                      variant="secondary"
                      className="h-7 shrink-0 px-2.5 text-caption"
                      onClick={() => onViewSchema(entry.name)}
                    >
                      View schema
                    </Button>
                    {typeof path === "string" ? (
                      <Button
                        variant="secondary"
                        className="h-7 shrink-0 px-2.5 text-caption"
                        onClick={() => onOpenTerminal(path)}
                      >
                        Terminal
                      </Button>
                    ) : null}
                  </div>
                ) : null}
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
