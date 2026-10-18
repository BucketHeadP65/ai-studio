"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import type { ReactElement } from "react";

import { inspectionApi } from "@/api/inspection";
import { inspectionKeys } from "@/api/query-keys";
import { studioErrorMessage } from "@/api/studio";
import { FindingDetailView } from "@/components/inspection/finding-detail";

// One finding, off `/inspection/{id}`: what is to blame, the evidence, the run, and the inspector's own
// conversation with a composer that continues it.

export default function FindingPage(): ReactElement {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const validId = Number.isInteger(id);

  const detail = useQuery({
    queryKey: inspectionKeys.finding(id),
    queryFn: () => inspectionApi.finding(id),
    enabled: validId,
  });

  if (!validId || detail.isError) {
    return (
      <p role="alert" className="mx-auto mt-3 w-fit rounded-md bg-danger-soft px-3 py-1.5 text-caption text-danger-ink">
        Could not open finding {params.id}. {detail.isError ? studioErrorMessage(detail.error) : null}
      </p>
    );
  }

  if (!detail.data) {
    return <p className="text-body text-label-3">Opening finding {params.id}.</p>;
  }

  return <FindingDetailView detail={detail.data} />;
}
