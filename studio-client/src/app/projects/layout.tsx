"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Folder, Plus, X } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import type { ReactElement, ReactNode } from "react";
import { useEffect, useId, useState } from "react";

import { type Project, type ProjectListingEntry, projectKeys, projectsApi } from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";
import { ActiveProjectProvider, useActiveProject } from "@/components/projects/active-project";
import { OpenFolderDialog } from "@/components/projects/open-folder-dialog";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui/button";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

interface ProjectCounts {
  files: number | null;
  openNotes: number | null;
}

function countWord(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

function summarize(projects: ProjectListingEntry[], counts: ProjectCounts[]): string {
  if (projects.length === 0) return "No projects yet.";
  let leaderIndex = -1;
  let leaderOpenNotes = 0;
  projects.forEach((_, index) => {
    const openNotes = counts[index]?.openNotes;
    if (openNotes !== null && openNotes !== undefined && openNotes > leaderOpenNotes) {
      leaderOpenNotes = openNotes;
      leaderIndex = index;
    }
  });
  const projectsClause = countWord(projects.length, "project");
  if (leaderIndex === -1) return `${projectsClause}.`;
  const leader = projects[leaderIndex];
  if (leader === undefined) return `${projectsClause}.`;
  return `${projectsClause}, ${countWord(leaderOpenNotes, "note")} open on ${leader.name}.`;
}

// The remove control sits beside the row's link, never inside it: a button inside an anchor is
// not valid markup, and a missing project (no folder on disk) still needs the control, since
// removing it from the workspace is the only thing left to do with it.
function RemoveButton({
  project,
  onRemove,
}: { project: ProjectListingEntry; onRemove: () => void }): ReactElement {
  return (
    <button
      type="button"
      onClick={onRemove}
      aria-label={`Remove ${project.name} from the workspace`}
      title="Remove from the workspace"
      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-label-3 opacity-0 transition-opacity hover:bg-well hover:text-label-1 focus-visible:opacity-100 group-hover:opacity-100"
    >
      <X aria-hidden size={14} />
    </button>
  );
}

function ProjectRow({
  project,
  isActive,
  counts,
  onRemove,
}: {
  project: ProjectListingEntry;
  isActive: boolean;
  counts: ProjectCounts;
  onRemove: () => void;
}): ReactElement {
  const subtitle =
    counts.files === null || counts.openNotes === null
      ? ""
      : `${countWord(counts.files, "file")}, ${countWord(counts.openNotes, "note")} open`;
  const rowClassName = cn(
    "flex min-w-0 flex-1 items-center gap-2.5 rounded-md px-3 py-2.5",
    isActive ? "bg-label-1" : "hover:bg-well",
  );

  if (project.missing) {
    return (
      <div className="group flex items-center gap-1">
        <div className={cn(rowClassName, "cursor-not-allowed opacity-50")}>
          <Folder aria-hidden size={16} className="shrink-0 text-label-3" />
          <div className="min-w-0 flex-1">
            <p className="truncate font-semibold text-body text-label-1">{project.name}</p>
            <p className="truncate text-caption text-label-3">Missing</p>
          </div>
        </div>
        <RemoveButton project={project} onRemove={onRemove} />
      </div>
    );
  }

  return (
    <div className="group flex items-center gap-1">
      <Link
        href={`/projects/${encodeURIComponent(project.id)}`}
        aria-current={isActive ? "page" : undefined}
        className={rowClassName}
      >
        <Folder
          aria-hidden
          size={16}
          className={cn("shrink-0", isActive ? "text-surface" : "text-label-3")}
        />
        <div className="min-w-0 flex-1">
          <p
            className={cn(
              "truncate font-semibold text-body",
              isActive ? "text-surface" : "text-label-1",
            )}
          >
            {project.name}
          </p>
          {subtitle === "" ? null : (
            <p
              className={cn(
                "truncate font-mono text-caption",
                isActive ? "text-surface/70" : "text-label-3",
              )}
            >
              {subtitle}
            </p>
          )}
        </div>
        {isActive && counts.openNotes !== null && counts.openNotes > 0 ? (
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-warn-ink font-mono text-micro text-surface font-semibold">
            {counts.openNotes}
          </span>
        ) : null}
      </Link>
      <RemoveButton project={project} onRemove={onRemove} />
    </div>
  );
}

function ProjectsShell({ children }: { children: ReactNode }): ReactElement {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { setActiveProjectId } = useActiveProject();
  const params = useParams<{ pid?: string }>();
  const currentPid = params.pid ? decodeURIComponent(params.pid) : null;
  const [dialogOpen, setDialogOpen] = useState(false);
  const railHeadingId = useId();

  const projectsQuery = useQuery({
    queryKey: projectKeys.all,
    queryFn: () => projectsApi.list(),
  });
  const projects = projectsQuery.data ?? [];
  const [pendingRemove, setPendingRemove] = useState<ProjectListingEntry | null>(null);
  const removeMutation = useMutation({
    mutationFn: (project: ProjectListingEntry) => projectsApi.unregister(project.id),
    onSuccess: (_, project) => {
      setPendingRemove(null);
      queryClient.invalidateQueries({ queryKey: projectKeys.all });
      if (project.id === currentPid) router.push("/projects");
    },
  });

  const filesQueries = useQueries({
    queries: projects.map((project) => ({
      queryKey: projectKeys.files(project.id),
      queryFn: () => projectsApi.files(project.id),
      staleTime: 60_000,
    })),
  });
  const notesQueries = useQueries({
    queries: projects.map((project) => ({
      queryKey: projectKeys.notes(project.id),
      queryFn: () => projectsApi.notes(project.id),
      staleTime: 60_000,
    })),
  });
  const counts: ProjectCounts[] = projects.map((_, index) => {
    const files = filesQueries[index]?.data;
    const notes = notesQueries[index]?.data;
    return {
      files: files ? files.filter((entry) => entry.kind === "file").length : null,
      openNotes: notes
        ? notes.filter((note) => !note.archived && note.status === "open").length
        : null,
    };
  });

  useEffect(() => {
    if (currentPid !== null) setActiveProjectId(currentPid);
  }, [currentPid, setActiveProjectId]);

  function opened(project: Project): void {
    queryClient.invalidateQueries({ queryKey: projectKeys.all });
    setActiveProjectId(project.id);
    setDialogOpen(false);
    router.push(`/projects/${encodeURIComponent(project.id)}`);
  }

  return (
    <div className="-m-6 flex h-[calc(100%+3rem)] flex-col">
      <div className="flex h-[70px] items-center gap-3 border-line border-b bg-surface px-7">
        <div className="min-w-0">
          <h1 className="font-bold text-label-1 text-title">Projects</h1>
          <p className="truncate text-caption text-label-3">
            {projectsQuery.isPending ? "" : summarize(projects, counts)}
          </p>
        </div>
        <div className="ml-auto">
          <Button onClick={() => setDialogOpen(true)}>
            <Plus aria-hidden size={14} className="mr-1.5 inline" />
            New project
          </Button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        <nav
          aria-labelledby={railHeadingId}
          className="flex w-[270px] shrink-0 flex-col gap-1 overflow-y-auto border-line border-r bg-surface px-3 py-3.5"
        >
          <h2
            id={railHeadingId}
            className="px-3 py-1 font-semibold text-label-3 text-micro uppercase tracking-caps"
          >
            All projects
          </h2>

          {projectsQuery.isPending ? (
            <p className="px-3 py-1 text-caption text-label-3">Loading projects.</p>
          ) : null}

          {projectsQuery.isError ? (
            <p
              role="alert"
              className="mx-1 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
            >
              {studioErrorMessage(projectsQuery.error)}
            </p>
          ) : null}

          {!projectsQuery.isPending && !projectsQuery.isError && projects.length === 0 ? (
            <p className="px-3 py-1 text-caption text-label-3">No projects yet.</p>
          ) : null}

          <ul className="flex flex-col gap-1">
            {projects.map((project, index) => (
              <li key={project.id}>
                <ProjectRow
                  project={project}
                  isActive={project.id === currentPid}
                  counts={counts[index] ?? { files: null, openNotes: null }}
                  onRemove={() => setPendingRemove(project)}
                />
              </li>
            ))}
          </ul>
        </nav>

        <div className="min-h-0 flex-1 overflow-auto px-7 pt-5 pb-7">{children}</div>
      </div>

      <OpenFolderDialog open={dialogOpen} onClose={() => setDialogOpen(false)} onOpened={opened} />
      <ConfirmDialog
        open={pendingRemove !== null}
        title={`Remove "${pendingRemove?.name ?? ""}" from the workspace?`}
        message="The folder and its files stay on disk. Sessions already recorded stay in history."
        confirmLabel="Remove"
        confirming={removeMutation.isPending}
        error={removeMutation.isError ? studioErrorMessage(removeMutation.error) : null}
        onConfirm={() => {
          if (pendingRemove !== null) removeMutation.mutate(pendingRemove);
        }}
        onCancel={() => {
          setPendingRemove(null);
          removeMutation.reset();
        }}
      />
    </div>
  );
}

export default function ProjectsLayout({ children }: { children: ReactNode }): ReactElement {
  return (
    <ActiveProjectProvider>
      <ProjectsShell>{children}</ProjectsShell>
    </ActiveProjectProvider>
  );
}
