"use client";

import { useQuery } from "@tanstack/react-query";
import { Select } from "client-core/ui/select";
import { type ReactElement, useId, useState } from "react";

import { inspectionApi } from "@/api/inspection";
import { runLocator, runName, spaced } from "@/api/inspection-words";
import { inspectionKeys } from "@/api/query-keys";
import { studioErrorMessage } from "@/api/studio";
import { countLine, filterFindings, type FindingsFilters } from "@/components/inspection/findings-filters";
import { FindingsTable } from "@/components/inspection/findings-table";

// Everything the detectors have found, newest first: a count line, four filters over the loaded list, and the
// table. The page loads the newest 200 findings once and narrows in the browser.

const ALL_FILTERS: FindingsFilters = { symptom: "", run: "", foundBy: "", days: 0 };

export default function InspectionPage(): ReactElement {
  const [filters, setFilters] = useState<FindingsFilters>(ALL_FILTERS);
  const symptomId = useId();
  const runId = useId();
  const foundById = useId();
  const timeId = useId();

  const findings = useQuery({
    queryKey: inspectionKeys.findings({ limit: 200 }),
    queryFn: () => inspectionApi.findings({ limit: 200 }),
  });

  const list = findings.data ?? [];
  const symptoms = Array.from(new Set(list.map((row) => row.symptom))).sort();
  const runs = Array.from(new Set(list.map((row) => runLocator(row.source)))).sort();

  return (
    <div className="flex flex-col gap-4">
      <header className="flex items-baseline gap-3">
        <h1 className="font-semibold text-label-1 text-title">Findings</h1>
        {findings.data ? <p className="text-body text-label-3">{countLine(list)}</p> : null}
      </header>

      <div className="flex flex-wrap gap-4">
        <label htmlFor={symptomId} className="flex items-center gap-2 text-caption text-label-3">
          Symptom
          <Select
            id={symptomId}
            aria-label="Symptom"
            value={filters.symptom}
            onChange={(event) => setFilters({ ...filters, symptom: event.target.value })}
          >
            <option value="">All</option>
            {symptoms.map((symptom) => (
              <option key={symptom} value={symptom}>
                {spaced(symptom)}
              </option>
            ))}
          </Select>
        </label>
        <label htmlFor={runId} className="flex items-center gap-2 text-caption text-label-3">
          Run or agent
          <Select
            id={runId}
            aria-label="Run or agent"
            value={filters.run}
            onChange={(event) => setFilters({ ...filters, run: event.target.value })}
          >
            <option value="">All</option>
            {runs.map((run) => {
              const { agent, short } = runName(run);
              return (
                <option key={run} value={run}>
                  {`${agent} ${short}`.trim()}
                </option>
              );
            })}
          </Select>
        </label>
        <label htmlFor={foundById} className="flex items-center gap-2 text-caption text-label-3">
          Found by
          <Select
            id={foundById}
            aria-label="Found by"
            value={filters.foundBy}
            onChange={(event) => setFilters({ ...filters, foundBy: event.target.value })}
          >
            <option value="">Rules and inspector</option>
            <option value="rule">Rules</option>
            <option value="inspector">Inspector</option>
          </Select>
        </label>
        <label htmlFor={timeId} className="flex items-center gap-2 text-caption text-label-3">
          Time
          <Select
            id={timeId}
            aria-label="Time"
            value={filters.days}
            onChange={(event) => setFilters({ ...filters, days: Number(event.target.value) })}
          >
            <option value={0}>All time</option>
            <option value={1}>Last day</option>
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
          </Select>
        </label>
      </div>

      {findings.isError ? (
        <p role="alert" className="w-fit rounded-md bg-danger-soft px-3 py-1.5 text-caption text-danger-ink">
          Could not read the findings. {studioErrorMessage(findings.error)}
        </p>
      ) : (
        <FindingsTable findings={filterFindings(list, filters)} loading={findings.isPending} />
      )}
    </div>
  );
}
