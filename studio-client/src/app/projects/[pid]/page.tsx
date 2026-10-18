"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import type { ReactElement } from "react";

import { projectKeys, projectsApi } from "@/api/projects";
import { Station } from "@/components/projects/station";

// The right pane for one project: its name and root path above today's Station (Files,
// Skills, Notes). The route's layout already marks this project active and owns the
// project list rail; this page only needs the one project's own record for its header.
export default function ProjectPage(): ReactElement {
  const params = useParams<{ pid: string }>();
  const projectId = decodeURIComponent(params.pid);

  const projectsQuery = useQuery({
    queryKey: projectKeys.all,
    queryFn: () => projectsApi.list(),
  });
  const project = projectsQuery.data?.find((entry) => entry.id === projectId) ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      <div className="min-w-0">
        <h1 className="truncate font-semibold text-label-1 text-title">
          {project?.name ?? projectId}
        </h1>
        <p className="truncate font-mono text-caption text-label-2">{project?.root_path ?? ""}</p>
      </div>
      <div className="min-h-0 flex-1">
        <Station projectId={projectId} />
      </div>
    </div>
  );
}
