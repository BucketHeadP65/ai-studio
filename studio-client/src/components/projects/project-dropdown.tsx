"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, FolderPlus } from "lucide-react";
import { useState } from "react";

import { type Project, projectKeys, projectsApi } from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";
import { cn } from "@/shared/lib/cn";
import { Badge } from "@/shared/ui/badge";

import { useActiveProject } from "./active-project";
import { OpenFolderDialog } from "./open-folder-dialog";

// The project switcher: every registered project, a running-sessions badge on
// the ones with a live session, missing ones dimmed and inert (their root_path
// no longer exists, nothing here can act on them), and the one way to add a
// project the backend doesn't already know about.
export function ProjectDropdown() {
  const queryClient = useQueryClient();
  const { activeProjectId, setActiveProjectId } = useActiveProject();
  const [menuOpen, setMenuOpen] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);

  const projects = useQuery({
    queryKey: projectKeys.all,
    queryFn: () => projectsApi.list(),
  });

  const active = (projects.data ?? []).find((project) => project.id === activeProjectId) ?? null;

  function choose(project: Project & { missing: boolean }) {
    if (project.missing) return;
    setActiveProjectId(project.id);
    setMenuOpen(false);
  }

  function opened(project: Project) {
    queryClient.invalidateQueries({ queryKey: projectKeys.all });
    setActiveProjectId(project.id);
    setDialogOpen(false);
  }

  return (
    <div className="relative inline-block text-left">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((value) => !value)}
        className="flex items-center gap-1.5 rounded-md border border-line bg-surface px-2.5 py-1.5 text-caption text-label-2 hover:bg-well"
      >
        <span className="max-w-40 truncate">
          {projects.isPending ? "Loading projects…" : (active?.name ?? "Select a project")}
        </span>
        <ChevronDown size={14} />
      </button>

      {projects.isError ? (
        <p
          role="alert"
          className="absolute top-full mt-1 whitespace-nowrap rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(projects.error)}
        </p>
      ) : null}

      {menuOpen ? (
        <div
          role="menu"
          aria-label="Projects"
          className="absolute top-full left-0 z-40 mt-1 min-w-56 rounded-md border border-line bg-surface py-1 shadow-lg"
        >
          {(projects.data ?? []).map((project) => (
            <button
              key={project.id}
              type="button"
              role="menuitemradio"
              aria-checked={project.id === activeProjectId}
              disabled={project.missing}
              onClick={() => choose(project)}
              className={cn(
                "flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption disabled:cursor-not-allowed",
                project.missing ? "text-label-3 opacity-50" : "text-label-1 hover:bg-well",
              )}
            >
              <span className="min-w-0 flex-1 truncate">{project.name}</span>
              {project.missing ? <span className="text-micro uppercase">missing</span> : null}
              {project.running_sessions > 0 ? (
                <Badge tone="accent">{project.running_sessions} running</Badge>
              ) : null}
            </button>
          ))}

          <div className="my-1 border-line border-t" />

          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setMenuOpen(false);
              setDialogOpen(true);
            }}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption text-label-2 hover:bg-well"
          >
            <FolderPlus size={14} />
            Open folder…
          </button>
        </div>
      ) : null}

      <OpenFolderDialog open={dialogOpen} onClose={() => setDialogOpen(false)} onOpened={opened} />
    </div>
  );
}
