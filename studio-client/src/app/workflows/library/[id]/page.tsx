"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import type { ReactElement } from "react";

import { studioApi, studioErrorMessage } from "@/api/studio";
import { WorkflowAbout } from "@/components/workflows/workflow-about";

// The "what it does" page for one saved workflow, off `/workflows/library/{id}`: its own
// description, applicability, steps in order, and what a run of it hands back -- distinct from
// the run inspector at `/workflows/{id}`, which reads a run rather than the artifact itself.

export default function WorkflowLibraryDetailPage(): ReactElement {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(params.id);

  const detail = useQuery({
    queryKey: ["studio", "workflow", id],
    queryFn: () => studioApi.workflow(id),
  });

  if (detail.isError) {
    return (
      <p
        role="alert"
        className="mx-auto mt-3 w-fit rounded-md bg-danger-soft px-3 py-1.5 text-caption text-danger-ink"
      >
        Could not open {id}. {studioErrorMessage(detail.error)}
      </p>
    );
  }

  if (!detail.data) {
    return <p className="text-body text-label-3">Loading {id}.</p>;
  }

  return (
    <div className="-m-6 flex flex-col">
      <WorkflowAbout detail={detail.data} />
    </div>
  );
}
