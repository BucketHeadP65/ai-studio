"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import type { ReactElement } from "react";

import { inspectionApi } from "@/api/inspection";
import { inspectionKeys } from "@/api/query-keys";
import { studioErrorMessage } from "@/api/studio";
import { RunOverviewView } from "@/components/inspection/run-overview";

// One run, whole, off `/inspection/runs/{run}`: everything the inspection did to it, top to bottom.

export default function RunOverviewPage(): ReactElement {
  const params = useParams<{ run: string }>();
  const runId = decodeURIComponent(params.run);

  const overview = useQuery({
    queryKey: inspectionKeys.run(runId),
    queryFn: () => inspectionApi.run(runId),
  });
  const outline = useQuery({
    queryKey: inspectionKeys.outline(runId),
    queryFn: () => inspectionApi.outline(runId),
  });

  if (overview.isError) {
    return (
      <p role="alert" className="mx-auto mt-3 w-fit rounded-md bg-danger-soft px-3 py-1.5 text-caption text-danger-ink">
        Could not open run {runId}. {studioErrorMessage(overview.error)}
      </p>
    );
  }

  if (!overview.data || outline.isPending) {
    return <p className="text-body text-label-3">Opening run {runId}.</p>;
  }

  return <RunOverviewView runId={runId} overview={overview.data} outline={outline.data ?? null} />;
}
