"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LayoutGrid, Search } from "lucide-react";
import Link from "next/link";
import { type ReactElement, useMemo, useState } from "react";

import { runKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import { studioApi, studioErrorMessage } from "@/api/studio";
import type { LibraryEntry } from "@/api/studio-types";
import { LibraryCard } from "@/components/workflows/library-card";
import { TRUST_LABEL } from "@/components/workflows/trust-chip";
import { Button } from "@/shared/ui/button";
import { EmptyState } from "@/shared/ui/empty-state";
import { Input } from "@/shared/ui/input";

// The browsable home for every saved workflow artifact, reachable until now only from chat.
// One card per artifact off `studioApi.library()` (filtered client side, the library is small),
// each opening the workflow or launching an unscoped run of it. A schedule for a workflow is
// created from the schedules station's own form, which picks its own workflow. Drafting or
// editing a spec stays the conversation's job: "New workflow" hands off to `/chat` rather than
// reimplementing any of that here.

const TRUST_LEVEL_ORDER = ["approved", "machine-confirmed", "unverified"] as const;

function matches(card: LibraryEntry, query: string): boolean {
  if (query === "") return true;
  const haystack = `${card.title} ${card.id} ${card.summary}`.toLowerCase();
  return haystack.includes(query);
}

// The header's one summary sentence: how many workflows sit in the library, split by the trust
// each has earned, in the order approved, confirmed by runs, not verified. A level none of the
// library's cards carry is left out rather than shown at zero.
function summarizeLibrary(cards: LibraryEntry[]): string {
  if (cards.length === 0) return "No workflows yet.";
  const counts: Record<string, number> = { approved: 0, "machine-confirmed": 0, unverified: 0 };
  for (const card of cards) {
    if (card.trust.level in counts) counts[card.trust.level] += 1;
  }
  const parts = [`${cards.length} workflow${cards.length === 1 ? "" : "s"}`];
  for (const level of TRUST_LEVEL_ORDER) {
    const count = counts[level] ?? 0;
    if (count > 0) parts.push(`${count} ${TRUST_LABEL[level]}`);
  }
  return `${parts.join(", ")}.`;
}

export function WorkflowLibrary(): ReactElement {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);

  const library = useQuery({ queryKey: ["library"], queryFn: () => studioApi.library() });
  const archivedLibrary = useQuery({
    queryKey: ["library", "archived"],
    queryFn: () => studioApi.library({ archived: true }),
    enabled: showArchived,
  });
  const runsQuery = useQuery({
    queryKey: runKeys.live,
    queryFn: () => sessionsApi.runs(),
  });

  function invalidateLibrary() {
    queryClient.invalidateQueries({ queryKey: ["library"] });
  }

  const restoreWorkflow = useMutation({
    mutationFn: (workflowId: string) => studioApi.restoreWorkflow(workflowId),
    onSuccess: invalidateLibrary,
  });

  const cards = library.data ?? [];
  const normalizedQuery = query.trim().toLowerCase();
  const filtered = useMemo(
    () => cards.filter((card) => matches(card, normalizedQuery)),
    [cards, normalizedQuery],
  );
  const archivedCards = archivedLibrary.data ?? [];
  const liveRuns = runsQuery.data ?? [];

  function restore(workflowId: string) {
    restoreWorkflow.mutate(workflowId);
  }

  const restoring = (workflowId: string) =>
    restoreWorkflow.isPending && restoreWorkflow.variables === workflowId;

  return (
    <div className="-m-6 flex flex-col">
      <div className="flex h-[70px] items-center gap-3 border-line border-b bg-surface px-7">
        <div className="min-w-0">
          <h1 className="font-bold text-label-1 text-title">Library</h1>
          {library.data ? (
            <p className="truncate text-caption text-label-3">{summarizeLibrary(cards)}</p>
          ) : null}
        </div>
        <div className="ml-auto flex items-center gap-2.5">
          <div className="relative w-60">
            <Search
              aria-hidden="true"
              size={15}
              className="-translate-y-1/2 absolute top-1/2 left-2.5 text-label-3"
            />
            <Input
              aria-label="Find a workflow"
              placeholder="Find a workflow"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              className="pl-8"
            />
          </div>
          <Button
            variant="secondary"
            aria-pressed={showArchived}
            onClick={() => setShowArchived((value) => !value)}
            className="h-8 px-4 text-caption"
          >
            {showArchived ? "Hide archived" : "Show archived"}
          </Button>
          <Link
            href="/chat"
            className="rounded-full bg-label-1 px-4 py-1.5 text-center font-semibold text-caption text-surface transition-colors hover:bg-label-1/85"
          >
            New workflow
          </Link>
        </div>
      </div>

      <div className="flex flex-col gap-4 px-7 pt-5 pb-7">
        {library.isError ? (
          <p
            role="alert"
            className="rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
          >
            {studioErrorMessage(library.error)}
          </p>
        ) : null}

        {library.isPending ? <p className="text-body text-label-3">Loading the library.</p> : null}

        {library.data && cards.length === 0 ? (
          <EmptyState
            icon={<LayoutGrid size={20} strokeWidth={1.5} />}
            title="No workflows yet."
            hint="Nothing is stored in the library yet. Draft one with the agent to fill it."
          >
            <Link
              href="/chat"
              className="rounded-full bg-label-1 px-4 py-1.5 font-semibold text-caption text-surface transition-colors hover:bg-label-1/85"
            >
              New workflow
            </Link>
          </EmptyState>
        ) : null}

        {library.data && cards.length > 0 && filtered.length === 0 ? (
          <p className="text-body text-label-3">No workflow matches your search.</p>
        ) : null}

        {filtered.length > 0 ? (
          <ul className="grid grid-cols-3 gap-3.5">
            {filtered.map((card) => (
              <LibraryCard
                key={card.id}
                card={card}
                liveRuns={liveRuns}
                archived={false}
                onRestore={restore}
                restoring={restoring(card.id)}
              />
            ))}
          </ul>
        ) : null}

        {showArchived ? (
          <div className="flex flex-col gap-3">
            <h2 className="font-semibold text-label-1 text-subtitle">Archived</h2>
            {archivedLibrary.isError ? (
              <p
                role="alert"
                className="rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
              >
                {studioErrorMessage(archivedLibrary.error)}
              </p>
            ) : null}
            {archivedLibrary.isPending ? (
              <p className="text-body text-label-3">Loading the archive.</p>
            ) : null}
            {archivedLibrary.data && archivedCards.length === 0 ? (
              <p className="text-body text-label-3">No workflows are archived.</p>
            ) : null}
            {archivedCards.length > 0 ? (
              <ul className="grid grid-cols-3 gap-3.5">
                {archivedCards.map((card) => (
                  <LibraryCard
                    key={card.id}
                    card={card}
                    liveRuns={liveRuns}
                    archived={true}
                    onRestore={restore}
                    restoring={restoring(card.id)}
                  />
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
