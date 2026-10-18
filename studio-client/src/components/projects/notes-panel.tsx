"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { type NoteListingEntry, projectKeys, projectsApi } from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";
import { StatePill } from "@/components/marks/state-pill";

export interface NotesPanelProps {
  projectId: string;
  onOpen: (path: string, line: number | null, noteId: string) => void;
}

function groupByPath(notes: NoteListingEntry[]): Array<[string, NoteListingEntry[]]> {
  const groups = new Map<string, NoteListingEntry[]>();
  for (const note of notes) {
    const bucket = groups.get(note.path) ?? [];
    bucket.push(note);
    groups.set(note.path, bucket);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([path, pathNotes]) => [path, sortOpenFirst(pathNotes)]);
}

// Open threads first, so a file with a live conversation reads before its settled ones;
// stable within each status, so notes otherwise keep the listing's own order.
function sortOpenFirst(notes: NoteListingEntry[]): NoteListingEntry[] {
  return [...notes].sort(
    (a, b) => Number(a.status === "resolved") - Number(b.status === "resolved"),
  );
}

// The Notes tab: every note in the project, grouped by file with open threads listed
// before resolved ones, archived notes hidden until the toggle reveals them, and a jump
// action that hands the host a (path, line) to open.
export function NotesPanel({ projectId, onOpen }: NotesPanelProps) {
  const [showArchived, setShowArchived] = useState(false);

  const notes = useQuery({
    queryKey: projectKeys.notes(projectId),
    queryFn: () => projectsApi.notes(projectId),
  });

  const allNotes = notes.data ?? [];
  const visible = showArchived ? allNotes : allNotes.filter((note) => !note.archived);
  const groups = groupByPath(visible);

  return (
    <div>
      <label className="flex items-center gap-2 text-caption text-label-2">
        <input
          type="checkbox"
          checked={showArchived}
          onChange={(event) => setShowArchived(event.target.checked)}
        />
        Show archived
      </label>

      {notes.isError ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(notes.error)}
        </p>
      ) : null}

      {notes.isPending ? (
        <p className="mt-2 text-caption text-label-3">Loading notes…</p>
      ) : groups.length === 0 ? (
        <p className="mt-2 text-caption text-label-3">No notes yet.</p>
      ) : (
        groups.map(([path, pathNotes]) => (
          <section key={path} className="mt-3">
            <h3 className="truncate font-mono text-caption text-label-2">{path}</h3>
            <ul className="mt-1 flex flex-col gap-0.5">
              {pathNotes.map(({ id, line, path: notePath, text, orphaned, status }) => (
                <li
                  key={id}
                  className={`flex items-center gap-2 rounded-md px-1 py-1 text-caption hover:bg-well ${status === "resolved" ? "opacity-70" : ""}`}
                >
                  <span className="shrink-0 font-mono text-label-3">
                    {line === null ? "L?" : `L${line}`}
                  </span>
                  {/* The primary "click on a note" control: opens the file and, once its
                      anchor is on screen, that note's own thread. */}
                  <button
                    type="button"
                    onClick={() => onOpen(notePath, line, id)}
                    className="min-w-0 flex-1 truncate text-left text-label-1"
                  >
                    {text}
                  </button>
                  {status === "resolved" ? (
                    <StatePill tone="completed" size="sm">
                      Resolved
                    </StatePill>
                  ) : null}
                  {orphaned ? (
                    <StatePill tone="warning" size="sm">
                      Anchor lost
                    </StatePill>
                  ) : null}
                  {/* A lost anchor has no line to jump to, so the action goes with it. */}
                  {line === null ? null : (
                    <button
                      type="button"
                      onClick={() => onOpen(notePath, line, id)}
                      className="shrink-0 text-accent hover:text-accent-ink"
                    >
                      Jump
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
    </div>
  );
}
