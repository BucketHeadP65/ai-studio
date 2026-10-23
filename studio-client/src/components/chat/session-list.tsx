"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, MessageSquare, Plus, Search, X } from "lucide-react";
import type { ReactElement } from "react";
import { useEffect, useState } from "react";

import { sessionKeys } from "@/api/query-keys";
import { sessionsApi } from "@/api/sessions";
import type { SessionSummary } from "@/api/sessions-types";
import { studioErrorMessage } from "@/api/studio";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui/button";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { EmptyState } from "@/shared/ui/empty-state";
import { Input } from "@/shared/ui/input";

import { SessionItem } from "./session-item";

// The left panel: every session, grouped by the project it belongs to, each
// group's own sessions newest first, the groups themselves ordered by their
// newest session. Search and the archived toggle drive the list query's own
// filters; rename, archive/unarchive, and delete each PATCH or DELETE through
// `sessionsApi` and invalidate the list on success. A single delete and a
// bulk delete each ask first through the shared ConfirmDialog; a failure (an
// answering session refusing deletion) closes the dialog and surfaces the
// backend's own sentence in the error banner.

export interface SessionListProps {
  viewedId: string | null;
  onView: (id: string | null) => void;
  onNewChat: () => void;
}

interface SessionGroup {
  projectId: string;
  name: string;
  sessions: SessionSummary[];
}

function newestActivity(group: SessionGroup): number {
  const newest = group.sessions[0];
  return newest ? new Date(newest.last_activity_at).getTime() : 0;
}

// Groups by project, each group's own sessions newest first; the groups
// themselves are then sorted by their newest session, descending, so a
// project with more recent activity leads regardless of where its sessions
// happened to sit in the raw list the backend returned.
function groupByProject(
  sessions: SessionSummary[],
  projectNames: Map<string, string>,
): SessionGroup[] {
  const byProject = new Map<string, SessionSummary[]>();
  for (const session of sessions) {
    const existing = byProject.get(session.project_id) ?? [];
    existing.push(session);
    byProject.set(session.project_id, existing);
  }
  const groups: SessionGroup[] = Array.from(byProject.entries()).map(([projectId, list]) => ({
    projectId,
    name: projectNames.get(projectId) ?? projectId,
    sessions: [...list].sort(
      (a, b) => new Date(b.last_activity_at).getTime() - new Date(a.last_activity_at).getTime(),
    ),
  }));
  return groups.sort((a, b) => newestActivity(b) - newestActivity(a));
}

export function SessionList({ viewedId, onView, onNewChat }: SessionListProps): ReactElement {
  const queryClient = useQueryClient();
  const [searchInput, setSearchInput] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkWorking, setBulkWorking] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);

  useEffect(() => {
    const handle = setTimeout(() => setDebouncedQuery(searchInput), 300);
    return () => clearTimeout(handle);
  }, [searchInput]);

  // Closing the search clears it: a hidden filter quietly narrowing the list would read as
  // sessions gone missing.
  function closeSearch() {
    setSearchOpen(false);
    setSearchInput("");
  }

  const projectsQuery = useQuery({
    queryKey: sessionKeys.projects,
    queryFn: () => sessionsApi.projects(),
  });
  const listQuery = useQuery({
    queryKey: sessionKeys.list(false, debouncedQuery),
    queryFn: () =>
      sessionsApi.list({
        archived: false,
        q: debouncedQuery === "" ? undefined : debouncedQuery,
      }),
  });
  const archivedQuery = useQuery({
    queryKey: sessionKeys.list(true, debouncedQuery),
    queryFn: () =>
      sessionsApi.list({
        archived: true,
        q: debouncedQuery === "" ? undefined : debouncedQuery,
      }),
    enabled: showArchived,
  });

  const sessions = listQuery.data ?? [];
  const projectNames = new Map(
    (projectsQuery.data ?? []).map((project) => [project.id, project.name]),
  );
  const groups = groupByProject(sessions, projectNames);
  // The backend's archived=true returns the WHOLE set (active included), so the
  // archived section keeps only the rows that are actually archived.
  const archivedSessions = (archivedQuery.data ?? []).filter((session) => session.archived);
  const archivedGroups = showArchived ? groupByProject(archivedSessions, projectNames) : [];
  // Every session the rail is showing right now, the archived section included
  // when it is open: what "Select all" ticks and what "all selected" is measured against.
  const shownIds = [...groups, ...archivedGroups].flatMap((group) =>
    group.sessions.map((session) => session.id),
  );
  const allShownSelected = shownIds.length > 0 && shownIds.every((id) => selectedIds.has(id));

  function invalidateList() {
    queryClient.invalidateQueries({ queryKey: ["sessions", "list"] });
  }

  async function handleRename(id: string, title: string) {
    try {
      await sessionsApi.rename(id, title);
      invalidateList();
    } catch (error) {
      setActionError(studioErrorMessage(error));
    }
  }

  async function handleToggleArchived(id: string, archived: boolean) {
    try {
      await sessionsApi.setArchived(id, archived);
      invalidateList();
    } catch (error) {
      setActionError(studioErrorMessage(error));
    }
  }

  function toggleSelect(id: string) {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function selectAllShown() {
    setSelectedIds(new Set(shownIds));
  }

  function clearSelection() {
    setSelectedIds(new Set());
  }

  function requestDelete(id: string) {
    const target = sessions.find((session) => session.id === id) ?? null;
    setDeleteTarget(target);
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await sessionsApi.remove(deleteTarget.id);
      const deletedId = deleteTarget.id;
      setDeleteTarget(null);
      invalidateList();
      if (viewedId === deletedId) onView(null);
    } catch (error) {
      setDeleteTarget(null);
      setActionError(studioErrorMessage(error));
    } finally {
      setDeleting(false);
    }
  }

  async function bulkArchive() {
    setBulkWorking(true);
    try {
      for (const id of selectedIds) {
        await sessionsApi.setArchived(id, true);
      }
    } catch (error) {
      setActionError(studioErrorMessage(error));
    } finally {
      invalidateList();
      setSelectedIds(new Set());
      setBulkWorking(false);
    }
  }

  async function confirmBulkDelete() {
    setBulkWorking(true);
    try {
      for (const id of selectedIds) {
        await sessionsApi.remove(id);
      }
    } catch (error) {
      setActionError(studioErrorMessage(error));
    } finally {
      invalidateList();
      setSelectedIds(new Set());
      setBulkWorking(false);
      setBulkDeleteOpen(false);
    }
  }

  function renderGroups(groupList: ReturnType<typeof groupByProject>) {
    return groupList.map((group) => (
      // biome-ignore lint/a11y/useSemanticElements: a project grouping, not a form control set.
      <div key={group.projectId} role="group" aria-label={group.name} className="mb-3">
        <h3 className="px-1 py-1 font-semibold text-label-3 text-micro uppercase tracking-caps">
          {group.name}
        </h3>
        <ul className="space-y-0.5">
          {group.sessions.map((session) => (
            <SessionItem
              key={session.id}
              session={session}
              viewed={session.id === viewedId}
              answering={session.answering}
              selected={selectedIds.has(session.id)}
              selecting={selectedIds.size > 0}
              onView={onView}
              onToggleSelect={toggleSelect}
              onRename={handleRename}
              onToggleArchived={handleToggleArchived}
              onDeleteRequest={requestDelete}
            />
          ))}
        </ul>
      </div>
    ));
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-0.5 px-2 pt-1.5 pb-0.5">
        {searchOpen ? (
          <Input
            aria-label="Search sessions"
            placeholder="Search sessions"
            value={searchInput}
            autoFocus
            onChange={(event) => setSearchInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") closeSearch();
            }}
            className="h-7 flex-1"
          />
        ) : (
          <button
            type="button"
            onClick={onNewChat}
            className="flex h-7 flex-1 items-center gap-1.5 rounded-md px-1.5 text-caption text-label-2 transition-colors hover:bg-well hover:text-label-1"
          >
            <Plus aria-hidden size={14} strokeWidth={1.8} />
            New chat
          </button>
        )}
        <button
          type="button"
          aria-label={searchOpen ? "Close search" : "Search sessions"}
          aria-expanded={searchOpen}
          onClick={() => (searchOpen ? closeSearch() : setSearchOpen(true))}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-label-3 transition-colors hover:bg-well hover:text-label-2"
        >
          {searchOpen ? (
            <X aria-hidden size={14} strokeWidth={1.8} />
          ) : (
            <Search aria-hidden size={14} strokeWidth={1.8} />
          )}
        </button>
        <button
          type="button"
          aria-label="Show archived"
          aria-pressed={showArchived}
          title="Show archived"
          onClick={() => setShowArchived((value) => !value)}
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors hover:bg-well",
            showArchived ? "bg-well text-label-1" : "text-label-3 hover:text-label-2",
          )}
        >
          <Archive aria-hidden size={14} strokeWidth={1.8} />
        </button>
      </div>

      {selectedIds.size > 0 ? (
        <div className="flex items-center justify-between gap-2 border-line border-b bg-well px-2.5 py-1.5">
          <span className="text-caption text-label-2">{selectedIds.size} selected</span>
          <div className="flex gap-1.5">
            {allShownSelected ? (
              <Button
                variant="secondary"
                disabled={bulkWorking}
                onClick={clearSelection}
                className="h-7 px-2.5 text-caption"
              >
                Clear
              </Button>
            ) : (
              <Button
                variant="secondary"
                disabled={bulkWorking}
                onClick={selectAllShown}
                className="h-7 px-2.5 text-caption"
              >
                Select all
              </Button>
            )}
            <Button
              variant="secondary"
              disabled={bulkWorking}
              onClick={bulkArchive}
              className="h-7 px-2.5 text-caption"
            >
              Archive
            </Button>
            <Button
              variant="danger"
              disabled={bulkWorking}
              onClick={() => setBulkDeleteOpen(true)}
              className="h-7 px-2.5 text-caption"
            >
              Delete
            </Button>
          </div>
        </div>
      ) : null}

      {actionError ? (
        <p role="alert" className="border-line border-b px-2.5 py-1.5 text-caption text-danger-ink">
          {actionError}
        </p>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {listQuery.isError ? (
          <p role="alert" className="px-1 py-2 text-caption text-danger-ink">
            Could not reach sessions, {studioErrorMessage(listQuery.error)}
          </p>
        ) : listQuery.isPending ? (
          <p className="px-1 py-2 text-caption text-label-3">Loading sessions.</p>
        ) : sessions.length === 0 && !showArchived ? (
          <EmptyState
            icon={<MessageSquare size={20} strokeWidth={1.5} />}
            title="No sessions yet. Say something."
          />
        ) : (
          <>
            {renderGroups(groups)}
            {showArchived ? (
              // biome-ignore lint/a11y/useSemanticElements: an archive grouping, not a form control set.
              <div role="group" aria-label="Archived" className="mt-4 border-line border-t pt-3">
                <h3 className="px-1 pb-1 font-semibold text-label-3 text-micro uppercase tracking-caps">
                  Archived
                </h3>
                {archivedQuery.isPending ? (
                  <p className="px-1 py-2 text-caption text-label-3">Loading archived sessions.</p>
                ) : archivedGroups.length === 0 ? (
                  <p className="px-1 py-2 text-caption text-label-3">No archived sessions.</p>
                ) : (
                  renderGroups(archivedGroups)
                )}
              </div>
            ) : null}
          </>
        )}
      </div>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete session"
        message={deleteTarget ? `Delete ${deleteTarget.title}? This cannot be undone.` : ""}
        confirmLabel="Delete"
        destructive
        confirming={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setDeleteTarget(null)}
      />

      <ConfirmDialog
        open={bulkDeleteOpen}
        title="Delete sessions"
        message={`Delete ${selectedIds.size} session${selectedIds.size === 1 ? "" : "s"}? This cannot be undone.`}
        confirmLabel="Delete"
        destructive
        confirming={bulkWorking}
        onConfirm={confirmBulkDelete}
        onCancel={() => setBulkDeleteOpen(false)}
      />
    </div>
  );
}
