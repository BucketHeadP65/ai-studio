"use client";

import { cn } from "@/shared/lib/cn";
import { formatRelative } from "@/shared/lib/format-relative";
import { Archive, ArchiveRestore, Check, MoreHorizontal, Pencil, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { SessionSummary } from "@/api/sessions-types";

export interface SessionItemProps {
  session: SessionSummary;
  viewed: boolean;
  answering: boolean;
  selected: boolean;
  // True while any session in the list is selected: every row then shows its
  // checkbox, so the next pick is one click rather than a hover first.
  selecting: boolean;
  onView: (id: string) => void;
  onToggleSelect: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onToggleArchived: (id: string, archived: boolean) => void;
  onDeleteRequest: (id: string) => void;
}

// One session row: title, relative activity, turn count. Rename edits the
// title in place; archive and delete hand off to the list, which owns the
// mutation and, for delete, the confirm step.
export function SessionItem({
  session,
  viewed,
  answering,
  selected,
  selecting,
  onView,
  onToggleSelect,
  onRename,
  onToggleArchived,
  onDeleteRequest,
}: SessionItemProps) {
  const [editing, setEditing] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [title, setTitle] = useState(session.title);
  const renameInputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // The open menu closes on any press outside it, so a stale menu never lingers
  // over a different row.
  useEffect(() => {
    if (!menuOpen) return;
    function onOutside(event: MouseEvent) {
      if (
        menuRef.current &&
        event.target instanceof Node &&
        !menuRef.current.contains(event.target)
      ) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("mousedown", onOutside);
    return () => document.removeEventListener("mousedown", onOutside);
  }, [menuOpen]);

  useEffect(() => {
    setTitle(session.title);
  }, [session.title]);

  useEffect(() => {
    if (editing) renameInputRef.current?.focus();
  }, [editing]);

  function commitRename() {
    const trimmed = title.trim();
    setEditing(false);
    if (trimmed.length > 0 && trimmed !== session.title) {
      onRename(session.id, trimmed);
    } else {
      setTitle(session.title);
    }
  }

  function cancelRename() {
    setTitle(session.title);
    setEditing(false);
  }

  return (
    // The whole card views the session; clicks on the row's own controls
    // (checkbox, rename, archive, delete) keep their own meaning.
    // biome-ignore lint/a11y/useKeyWithClickEvents: the li is not focusable, so no key event can reach it; the keyboard path is the title button inside.
    <li
      onClick={(event) => {
        if (editing) return;
        if (event.target instanceof Element && event.target.closest("button, input, a")) return;
        onView(session.id);
      }}
      className={cn(
        "group flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 transition-colors",
        viewed ? "bg-well" : "hover:bg-well",
      )}
    >
      <input
        type="checkbox"
        aria-label={`Select ${session.title}`}
        checked={selected}
        onChange={() => onToggleSelect(session.id)}
        className={cn(
          "invisible shrink-0 group-hover:visible",
          (selected || selecting) && "visible",
        )}
      />

      <div className="min-w-0 flex-1">
        {editing ? (
          <input
            ref={renameInputRef}
            aria-label="Rename session"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") commitRename();
              if (event.key === "Escape") cancelRename();
            }}
            className="w-full rounded border border-line bg-surface px-1.5 py-0.5 text-body text-label-1"
          />
        ) : (
          <button
            type="button"
            onClick={() => onView(session.id)}
            aria-current={viewed ? "true" : undefined}
            title={`${session.title}, ${formatRelative(session.last_activity_at)}, ${session.turn_count} turn${session.turn_count === 1 ? "" : "s"}`}
            className="block w-full text-left"
          >
            <div className="flex items-center gap-1.5">
              <span
                className={cn(
                  "min-w-0 flex-1 truncate text-body",
                  viewed ? "font-medium text-label-1" : "text-label-2",
                )}
              >
                {session.title}
              </span>
              {answering ? (
                <span
                  role="img"
                  aria-label="Answering"
                  className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent"
                />
              ) : null}
            </div>
          </button>
        )}
      </div>

      {editing ? (
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            aria-label="Save title"
            onClick={commitRename}
            className="rounded p-1 text-label-2 hover:bg-well"
          >
            <Check size={14} />
          </button>
          <button
            type="button"
            aria-label="Cancel rename"
            onClick={cancelRename}
            className="rounded p-1 text-label-2 hover:bg-well"
          >
            <X size={14} />
          </button>
        </div>
      ) : (
        <div
          ref={menuRef}
          className={cn(
            "invisible relative shrink-0 group-hover:visible",
            (viewed || menuOpen) && "visible",
          )}
        >
          <button
            type="button"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-label={`Session actions for ${session.title}`}
            onClick={() => setMenuOpen((value) => !value)}
            className="rounded p-1 text-label-2 hover:bg-well"
          >
            <MoreHorizontal size={14} />
          </button>
          {menuOpen ? (
            <div
              role="menu"
              aria-label={`Actions for ${session.title}`}
              className="absolute top-full right-0 z-40 mt-1 min-w-36 rounded-lg border border-line bg-surface py-1 shadow-lg"
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  setEditing(true);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption text-label-1 hover:bg-well"
              >
                <Pencil size={14} />
                Rename
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onToggleArchived(session.id, !session.archived);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption text-label-1 hover:bg-well"
              >
                {session.archived ? <ArchiveRestore size={14} /> : <Archive size={14} />}
                {session.archived ? "Unarchive" : "Archive"}
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onDeleteRequest(session.id);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption text-danger-ink hover:bg-danger-soft"
              >
                <Trash2 size={14} />
                Delete
              </button>
            </div>
          ) : null}
        </div>
      )}
    </li>
  );
}
