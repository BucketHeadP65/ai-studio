"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui/button";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { Input } from "@/shared/ui/input";
import { StatusDot } from "@/shared/ui/status-dot";
import {
  ChevronDown,
  ChevronRight,
  FilePlus,
  FolderPlus,
  MoreHorizontal,
  Pencil,
  Trash2,
} from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";

import { type FileEntry, type FileNoteSummary, projectKeys, projectsApi } from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";

import { type NoteDotTone, noteDotTone } from "./notes-seen";

// No push topic names a project's files: the agent's file tools write them outside every
// announcing path, so this tree is the one place that still polls, on both the listing and the
// per-file note-status summary, rather than waiting on a topic that will never arrive.
const TREE_REFRESH_MS = 4000;

interface FileTreeProps {
  projectId: string;
  /** Fired with a markdown file's path when it is clicked. */
  onSelectFile: (path: string) => void;
  selectedPath?: string | null;
  /** Fired with a path once it has been deleted, so a caller holding that path elsewhere can drop it. */
  onPathRemoved?: (path: string) => void;
  /** Fired with a path's old and new value once it has been renamed. */
  onPathRenamed?: (from: string, to: string) => void;
}

interface TreeNode {
  name: string;
  path: string;
  kind: FileEntry["kind"];
  children: TreeNode[];
}

function dirnameOf(path: string): string {
  const index = path.lastIndexOf("/");
  return index === -1 ? "" : path.slice(0, index);
}

function basenameOf(path: string): string {
  const index = path.lastIndexOf("/");
  return index === -1 ? path : path.slice(index + 1);
}

function joinPath(dir: string, name: string): string {
  return dir === "" ? name : `${dir}/${name}`;
}

function isMarkdownPath(path: string): boolean {
  return path.toLowerCase().endsWith(".md");
}

// Only markdown files are ever openable, so they are the only files worth
// listing; every other file, and every directory that has no markdown file
// anywhere beneath it, is dropped before the tree is built. `buildTree`
// synthesizes whatever ancestor directories a kept file still needs, so a
// directory survives exactly when at least one of its descendants does.
function markdownEntries(entries: FileEntry[]): FileEntry[] {
  return entries.filter((entry) => entry.kind === "file" && isMarkdownPath(entry.path));
}

function createFileLabel(dir: string): string {
  return dir === "" ? "New file at root" : `New file in ${dir}`;
}

function createDirLabel(dir: string): string {
  return dir === "" ? "New folder at root" : `New folder in ${dir}`;
}

// Nests the backend's flat {path, kind, size} listing into a tree by splitting
// each path on "/". A file's ancestor directories are synthesized on the fly
// if the listing didn't carry an explicit entry for them, so the tree renders
// correctly whether or not the backend lists directory stubs. Directories sort
// before files at every level, then both sort alphabetically by name.
function buildTree(entries: FileEntry[]): TreeNode[] {
  const roots: TreeNode[] = [];
  const byPath = new Map<string, TreeNode>();

  function attach(node: TreeNode): void {
    const parentPath = dirnameOf(node.path);
    if (parentPath === "") {
      roots.push(node);
      return;
    }
    attachOrGetDir(parentPath).children.push(node);
  }

  function attachOrGetDir(path: string): TreeNode {
    const existing = byPath.get(path);
    if (existing) return existing;
    const node: TreeNode = { name: basenameOf(path), path, kind: "dir", children: [] };
    byPath.set(path, node);
    attach(node);
    return node;
  }

  const sorted = [...entries].sort((a, b) => a.path.localeCompare(b.path));
  for (const entry of sorted) {
    if (byPath.has(entry.path)) continue;
    const node: TreeNode = {
      name: basenameOf(entry.path),
      path: entry.path,
      kind: entry.kind,
      children: [],
    };
    byPath.set(entry.path, node);
    attach(node);
  }

  function sortSiblings(nodes: TreeNode[]): void {
    nodes.sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    for (const node of nodes) sortSiblings(node.children);
  }
  sortSiblings(roots);

  return roots;
}

interface TreeActions {
  collapsedPaths: Set<string>;
  onToggle: (path: string) => void;
  selectedPath: string | null;
  onSelectFile: (path: string) => void;
  creatingIn: string | null;
  createKind: "file" | "dir";
  createName: string;
  onCreateNameChange: (value: string) => void;
  onStartCreate: (dir: string, kind: "file" | "dir") => void;
  onSubmitCreate: (dir: string) => void;
  onCancelCreate: () => void;
  createBusy: boolean;
  renamingPath: string | null;
  renameName: string;
  onRenameNameChange: (value: string) => void;
  onStartRename: (node: TreeNode) => void;
  onSubmitRename: (node: TreeNode) => void;
  onCancelRename: () => void;
  renameBusy: boolean;
  onRequestDelete: (path: string) => void;
  noteTone: (path: string) => NoteDotTone;
}

function InlineForm({
  label,
  value,
  submitLabel,
  busy,
  onChange,
  onSubmit,
  onCancel,
}: {
  label: string;
  value: string;
  submitLabel: string;
  busy: boolean;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  return (
    <form
      aria-label={label}
      className="flex flex-1 items-center gap-1 py-0.5"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <Input
        autoFocus
        value={value}
        disabled={busy}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
          }
        }}
        className="h-6 min-w-0 py-0.5 text-caption"
      />
      <Button
        type="submit"
        disabled={busy || value.trim() === ""}
        className="h-6 shrink-0 px-2 text-caption"
      >
        {submitLabel}
      </Button>
      <Button
        type="button"
        variant="secondary"
        disabled={busy}
        onClick={onCancel}
        className="h-6 shrink-0 px-2 text-caption"
      >
        Cancel
      </Button>
    </form>
  );
}

function IconButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      className="shrink-0 rounded p-1 text-label-3 hover:bg-well hover:text-label-1"
    >
      {children}
    </button>
  );
}

// The meaning is never carried by color alone: `title`/`aria-label` name it in words, and
// "new" additionally pulses, so the two states read apart even without color vision. `tone
// === "none"` (no notes at all) renders nothing -- a row with nothing to say stays quiet.
function NoteStatusDot({ tone }: { tone: NoteDotTone }) {
  if (tone === "none") return null;
  const label =
    tone === "new"
      ? "New agent activity in the notes of this file"
      : "This file has notes, and every agent reply on it has been seen";
  return (
    <span role="img" title={label} aria-label={label} className="inline-flex shrink-0 items-center">
      <StatusDot tone={tone === "new" ? "warn" : "ok"} pulse={tone === "new"} />
    </span>
  );
}

function RowMenu({
  node,
  actions,
}: {
  node: TreeNode;
  actions: TreeActions;
}) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // The open menu closes on any press outside it, so a stale menu never lingers
  // over a different row.
  useEffect(() => {
    if (!open) return;
    function onOutside(event: MouseEvent) {
      if (
        menuRef.current &&
        event.target instanceof Node &&
        !menuRef.current.contains(event.target)
      ) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onOutside);
    return () => document.removeEventListener("mousedown", onOutside);
  }, [open]);

  const isDir = node.kind === "dir";

  return (
    <div
      ref={menuRef}
      className={cn("invisible relative shrink-0 group-hover:visible", open && "visible")}
    >
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Actions for ${node.path}`}
        onClick={() => setOpen((value) => !value)}
        className="rounded p-1 text-label-3 hover:bg-well hover:text-label-1"
      >
        <MoreHorizontal size={13} />
      </button>
      {open ? (
        <div
          role="menu"
          aria-label={`Actions for ${node.path}`}
          className="absolute top-full right-0 z-40 mt-1 min-w-32 rounded-md border border-line bg-surface py-1 shadow-lg"
        >
          {isDir ? (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                actions.onStartCreate(node.path, "file");
              }}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption text-label-1 hover:bg-well"
            >
              <FilePlus size={13} />
              New file
            </button>
          ) : null}
          {isDir ? (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                actions.onStartCreate(node.path, "dir");
              }}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption text-label-1 hover:bg-well"
            >
              <FolderPlus size={13} />
              New folder
            </button>
          ) : null}
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              actions.onStartRename(node);
            }}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption text-label-1 hover:bg-well"
          >
            <Pencil size={13} />
            Rename
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              actions.onRequestDelete(node.path);
            }}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption text-danger-ink hover:bg-danger-soft"
          >
            <Trash2 size={13} />
            Delete
          </button>
        </div>
      ) : null}
    </div>
  );
}

function TreeNodeRow({
  node,
  depth,
  actions,
}: {
  node: TreeNode;
  depth: number;
  actions: TreeActions;
}) {
  const isDir = node.kind === "dir";
  const isExpanded = !actions.collapsedPaths.has(node.path);
  const isRenaming = actions.renamingPath === node.path;
  const isCreatingHere = isDir && actions.creatingIn === node.path;
  const isSelected = !isDir && actions.selectedPath === node.path;
  // Every file that survives the markdown filter is openable, so this is
  // just "not a directory" -- kept as its own name for what it means here.
  const clickable = !isDir;

  return (
    // biome-ignore lint/a11y/useFocusableInteractive: focus and keyboard handling live on the row's own button/link, not this row wrapper.
    <li role="treeitem" aria-expanded={isDir ? isExpanded : undefined} aria-selected={isSelected}>
      <div
        className={cn(
          "group flex h-6 items-center gap-1 rounded px-1",
          isSelected ? "bg-accent-soft" : "hover:bg-well",
        )}
        style={{ paddingLeft: depth * 12 }}
      >
        {isDir ? (
          <IconButton
            label={isExpanded ? `Collapse ${node.path}` : `Expand ${node.path}`}
            onClick={() => actions.onToggle(node.path)}
          >
            {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          </IconButton>
        ) : (
          <span aria-hidden className="inline-block w-[22px] shrink-0" />
        )}

        {isRenaming ? (
          <InlineForm
            label={`Rename ${node.path}`}
            value={actions.renameName}
            submitLabel="Save"
            busy={actions.renameBusy}
            onChange={actions.onRenameNameChange}
            onSubmit={() => actions.onSubmitRename(node)}
            onCancel={actions.onCancelRename}
          />
        ) : clickable ? (
          <button
            type="button"
            title={node.name}
            onClick={() => actions.onSelectFile(node.path)}
            className="min-w-0 flex-1 truncate text-left text-caption leading-tight text-label-1 hover:text-accent"
          >
            {node.name}
          </button>
        ) : (
          <span
            title={node.name}
            className={cn(
              "min-w-0 flex-1 truncate text-caption leading-tight",
              isDir ? "text-label-1" : "text-label-3",
            )}
          >
            {node.name}
          </span>
        )}

        {isRenaming || isDir ? null : <NoteStatusDot tone={actions.noteTone(node.path)} />}

        {isRenaming ? null : <RowMenu node={node} actions={actions} />}
      </div>

      {isCreatingHere ? (
        <div style={{ paddingLeft: (depth + 1) * 12 }}>
          <InlineForm
            label={
              actions.createKind === "dir" ? createDirLabel(node.path) : createFileLabel(node.path)
            }
            value={actions.createName}
            submitLabel="Create"
            busy={actions.createBusy}
            onChange={actions.onCreateNameChange}
            onSubmit={() => actions.onSubmitCreate(node.path)}
            onCancel={actions.onCancelCreate}
          />
        </div>
      ) : null}

      {isDir && isExpanded && node.children.length > 0 ? (
        // biome-ignore lint/a11y/useSemanticElements: role="group" nesting under role="treeitem" is the standard ARIA treeview pattern; <fieldset> is not a tree node.
        <ul role="group">
          {node.children.map((child) => (
            <TreeNodeRow key={child.path} node={child} depth={depth + 1} actions={actions} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

// A project's files as a collapsible tree, derived from the flat listing the backend returns.
// Markdown files are the only ones there is a viewer for, so they are the only ones listed at
// all: every non-markdown file, and every directory with no markdown file anywhere beneath it, is
// filtered out first. Every remaining node can be renamed or deleted, from its row's dot menu;
// directories additionally offer "New file" and "New folder" for a child there; the root offers
// file creation too, via the header button, for a project with no files yet. Every mutation
// re-fetches the listing on success; `onPathRemoved` and `onPathRenamed` additionally let a caller
// holding one of these paths elsewhere (a currently open file, say) react to it disappearing or
// moving. Each markdown row also carries a note-status dot, driven by a per-file summary polled
// alongside the listing: nothing when the file has no notes, a settled tone once every agent
// reply on it has been seen (recorded per-viewer in localStorage, see `notes-seen.ts`), a fresh
// tone the moment a new one hasn't.
export function FileTree({
  projectId,
  onSelectFile,
  selectedPath = null,
  onPathRemoved,
  onPathRenamed,
}: FileTreeProps) {
  const queryClient = useQueryClient();
  const filesKey = projectKeys.files(projectId);

  const filesQuery = useQuery({
    queryKey: filesKey,
    queryFn: () => projectsApi.files(projectId),
    refetchInterval: TREE_REFRESH_MS,
  });
  const tree = useMemo(() => buildTree(markdownEntries(filesQuery.data ?? [])), [filesQuery.data]);

  const summaryQuery = useQuery({
    queryKey: projectKeys.notesSummary(projectId),
    queryFn: () => projectsApi.notesSummary(projectId),
    refetchInterval: TREE_REFRESH_MS,
  });
  const summaryByPath = useMemo(() => {
    const map = new Map<string, FileNoteSummary>();
    for (const entry of summaryQuery.data ?? []) map.set(entry.path, entry);
    return map;
  }, [summaryQuery.data]);
  function noteTone(path: string): NoteDotTone {
    return noteDotTone(projectId, summaryByPath.get(path));
  }

  const [collapsedPaths, setCollapsedPaths] = useState<Set<string>>(new Set());
  const [creatingIn, setCreatingIn] = useState<string | null>(null);
  const [createKind, setCreateKind] = useState<"file" | "dir">("file");
  const [createName, setCreateName] = useState("");
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [renameName, setRenameName] = useState("");
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  function refresh(): void {
    queryClient.invalidateQueries({ queryKey: filesKey });
  }

  const createFileMutation = useMutation({
    mutationFn: (path: string) => projectsApi.createFile(projectId, path),
    onMutate: () => setActionError(null),
    onSuccess: () => {
      setCreatingIn(null);
      setCreateName("");
      refresh();
    },
    onError: (error: unknown) => setActionError(studioErrorMessage(error)),
  });

  const createDirMutation = useMutation({
    mutationFn: (path: string) => projectsApi.createDir(projectId, path),
    onMutate: () => setActionError(null),
    onSuccess: () => {
      setCreatingIn(null);
      setCreateName("");
      refresh();
    },
    onError: (error: unknown) => setActionError(studioErrorMessage(error)),
  });

  const renameMutation = useMutation({
    mutationFn: ({ path, to }: { path: string; to: string }) =>
      projectsApi.renameFile(projectId, path, to),
    onMutate: () => setActionError(null),
    onSuccess: (_data, variables) => {
      setRenamingPath(null);
      setRenameName("");
      refresh();
      onPathRenamed?.(variables.path, variables.to);
    },
    onError: (error: unknown) => setActionError(studioErrorMessage(error)),
  });

  const deleteMutation = useMutation({
    mutationFn: (path: string) => projectsApi.deleteFile(projectId, path),
    onMutate: () => setActionError(null),
    onSuccess: (_data, path) => {
      setPendingDelete(null);
      refresh();
      onPathRemoved?.(path);
    },
    onError: (error: unknown) => {
      setPendingDelete(null);
      setActionError(studioErrorMessage(error));
    },
  });

  function toggle(path: string): void {
    setCollapsedPaths((previous) => {
      const next = new Set(previous);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }

  function startCreate(dir: string, kind: "file" | "dir"): void {
    setActionError(null);
    setRenamingPath(null);
    setCreatingIn(dir);
    setCreateKind(kind);
    setCreateName("");
  }

  function cancelCreate(): void {
    setCreatingIn(null);
    setCreateName("");
  }

  function submitCreate(dir: string): void {
    const name = createName.trim();
    if (name === "") return;
    const path = joinPath(dir, name);
    if (createKind === "dir") {
      createDirMutation.mutate(path);
    } else {
      createFileMutation.mutate(path);
    }
  }

  function startRename(node: TreeNode): void {
    setActionError(null);
    setCreatingIn(null);
    setRenamingPath(node.path);
    setRenameName(node.name);
  }

  function cancelRename(): void {
    setRenamingPath(null);
    setRenameName("");
  }

  function submitRename(node: TreeNode): void {
    const name = renameName.trim();
    if (name === "") return;
    const to = joinPath(dirnameOf(node.path), name);
    if (to === node.path) {
      cancelRename();
      return;
    }
    renameMutation.mutate({ path: node.path, to });
  }

  const actions: TreeActions = {
    collapsedPaths,
    onToggle: toggle,
    selectedPath,
    onSelectFile,
    creatingIn,
    createKind,
    createName,
    onCreateNameChange: setCreateName,
    onStartCreate: startCreate,
    onSubmitCreate: submitCreate,
    onCancelCreate: cancelCreate,
    createBusy: createFileMutation.isPending || createDirMutation.isPending,
    renamingPath,
    renameName,
    onRenameNameChange: setRenameName,
    onStartRename: startRename,
    onSubmitRename: submitRename,
    onCancelRename: cancelRename,
    renameBusy: renameMutation.isPending,
    onRequestDelete: setPendingDelete,
    noteTone,
  };

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between">
        <h2 className="font-medium text-caption text-label-3 uppercase tracking-wider">Files</h2>
        <IconButton label={createFileLabel("")} onClick={() => startCreate("", "file")}>
          <FilePlus size={14} />
        </IconButton>
      </div>

      {creatingIn === "" ? (
        <InlineForm
          label={createKind === "dir" ? createDirLabel("") : createFileLabel("")}
          value={createName}
          submitLabel="Create"
          busy={createFileMutation.isPending || createDirMutation.isPending}
          onChange={setCreateName}
          onSubmit={() => submitCreate("")}
          onCancel={cancelCreate}
        />
      ) : null}

      {filesQuery.isPending ? <p className="text-caption text-label-3">Loading files…</p> : null}
      {filesQuery.isError ? (
        <p
          role="alert"
          className="rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {studioErrorMessage(filesQuery.error)}
        </p>
      ) : null}
      {actionError ? (
        <p
          role="alert"
          className="rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
        >
          {actionError}
        </p>
      ) : null}

      {filesQuery.data ? (
        tree.length === 0 ? (
          <p className="text-caption text-label-3">No files yet.</p>
        ) : (
          // biome-ignore lint/a11y/noNoninteractiveElementToInteractiveRole: the WAI-ARIA APG treeview pattern is ul/li carrying tree/treeitem.
          <ul role="tree" aria-label="Project files" className="flex flex-col gap-0.5">
            {tree.map((node) => (
              <TreeNodeRow key={node.path} node={node} depth={0} actions={actions} />
            ))}
          </ul>
        )
      ) : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        title={`Delete "${pendingDelete ?? ""}"?`}
        message="Removed from disk. This can't be undone."
        confirmLabel="Delete it"
        destructive
        confirming={deleteMutation.isPending}
        onConfirm={() => {
          if (pendingDelete !== null) deleteMutation.mutate(pendingDelete);
        }}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
