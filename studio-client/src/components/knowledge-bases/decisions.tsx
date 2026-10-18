"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge } from "client-core/ui/badge";
import { Button } from "client-core/ui/button";
import { Card } from "client-core/ui/card";
import { Input } from "client-core/ui/input";
import { useState } from "react";

import { knowledgeBaseApi, knowledgeBaseErrorMessage } from "@/api/knowledge-base";
import type { DecisionOutcome, DecisionRow } from "@/api/types";

interface Props {
  name: string;
}

function capitalize(value: string): string {
  return value.length === 0 ? value : value[0].toUpperCase() + value.slice(1);
}

// Every page awaiting a decision on this base, across whichever page types declare one: title,
// its page type and status as plain text, and one button per outcome the page type allows (its
// declared `outcomes_by_page_type`, keyed off `status` since neither name nor render any
// particular page type or status). An outcome carrying a link field shows a page-id input beside
// its button, sent as that field's whole new value. `skipped` names decision pages that failed to
// parse, so they stay visible even though they never made it into `rows`. Fetches and mutates on
// its own — the detail page mounts this with just a knowledge base name.
export function DecisionsCard({ name }: Props) {
  const queryClient = useQueryClient();
  const [linkValues, setLinkValues] = useState<Record<string, string>>({});

  const query = useQuery({
    queryKey: ["knowledge-base", name, "decisions"],
    queryFn: () => knowledgeBaseApi.decisions(name),
  });

  const decide = useMutation({
    mutationFn: ({
      row,
      outcome,
      linkValue,
    }: {
      row: DecisionRow;
      outcome: DecisionOutcome;
      linkValue: string;
    }) =>
      knowledgeBaseApi.decidePage(name, row.id, {
        status: outcome.status,
        ...(outcome.link ? { links: { [outcome.link]: [linkValue] } } : {}),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["knowledge-base", name, "decisions"] });
    },
  });

  const rows = query.data?.rows ?? [];
  const skipped = query.data?.skipped ?? [];
  const outcomesByPageType = query.data?.outcomes_by_page_type ?? {};

  return (
    <Card className="px-4 py-3">
      <h2 className="font-medium text-sm text-label-1">Decisions</h2>
      <p className="mt-0.5 text-caption text-label-3">Pages awaiting a decision on this base.</p>

      {query.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {knowledgeBaseErrorMessage(query.error)}
        </p>
      ) : null}

      {decide.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {knowledgeBaseErrorMessage(decide.error)}
        </p>
      ) : null}

      {skipped.length > 0 ? (
        <p className="mt-2 text-caption text-label-3">
          {skipped.length} page{skipped.length === 1 ? "" : "s"} could not be read (
          {skipped.join(", ")}).
        </p>
      ) : null}

      {query.isPending ? (
        <p className="mt-3 text-caption text-label-3">Loading.</p>
      ) : rows.length === 0 ? (
        <p className="mt-3 text-caption text-label-3">Nothing awaits a decision.</p>
      ) : (
        <ul className="mt-3 divide-y divide-line">
          {rows.map((row) => {
            const outcomes = outcomesByPageType[row.page_type] ?? [];
            return (
              <li key={row.id} className="py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-body text-label-1">{row.title}</span>
                  <Badge tone="neutral" className="normal-case">
                    {row.page_type}
                  </Badge>
                  <Badge tone="neutral" className="normal-case">
                    {row.status}
                  </Badge>
                  <span className="flex flex-1 flex-wrap justify-end gap-2">
                    {outcomes.map((outcome) => {
                      const linkKey = `${row.id}:${outcome.status}`;
                      return (
                        <span key={outcome.status} className="flex items-center gap-1">
                          {outcome.link ? (
                            <Input
                              value={linkValues[linkKey] ?? ""}
                              onChange={(event) =>
                                setLinkValues((current) => ({
                                  ...current,
                                  [linkKey]: event.target.value,
                                }))
                              }
                              placeholder="page id"
                              aria-label={`${row.title} page id`}
                              className="h-7 w-40 py-0 text-caption"
                            />
                          ) : null}
                          <Button
                            className="h-7 px-2.5 text-caption"
                            onClick={() =>
                              decide.mutate({ row, outcome, linkValue: linkValues[linkKey] ?? "" })
                            }
                            disabled={decide.isPending}
                          >
                            {capitalize(outcome.status)}
                          </Button>
                        </span>
                      );
                    })}
                  </span>
                </div>
                <p className="mt-1 text-caption text-label-2">{row.summary}</p>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
