"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { cn } from "@/shared/lib/cn";
import { Button } from "@/shared/ui/button";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { EmptyState } from "@/shared/ui/empty-state";
import { FileText } from "lucide-react";
import type { MouseEvent, RefObject } from "react";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  type FileContent,
  type NoteListingEntry,
  type SkillCard,
  projectKeys,
  projectsApi,
} from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";
import { Rail } from "@/components/panels/rail";
import { useStoredFlag } from "@/lib/use-stored";

import { FileTree } from "./file-tree";
import { MarkdownEditor } from "./markdown-editor";
import {
  MarkdownView,
  type MarkdownViewProps,
  type SelectionPosition,
  rangeEndPosition,
} from "./markdown-view";
import { NoteMargin } from "./note-margin";
import { AddNoteButton, NotePopover, ThreadPopover } from "./note-popover";
import { NotesPanel } from "./notes-panel";
import { markSeen } from "./notes-seen";
import { SkillsBrowser } from "./skills-browser";
import {
  blocksForSourceSpan,
  elementCoveringOffset,
  hasLayout,
  lineStartOffset,
  narrowestCoveringSpan,
  rangeForSourceSpan,
  sourceOffsetOfDomPoint,
} from "./source-positions";
import { stampProseTheme, useAppIsDark } from "./use-app-dark";
import { pathFromWikilinkHref, resolveWikilinkTarget, wikilinkHref } from "./wikilinks";

/** Registered name for every note's precise-character highlight under the CSS Custom Highlight API. */
const NOTE_HIGHLIGHT_NAME = "engram-note";

/** Whether the CSS Custom Highlight API is available -- jsdom (and older browsers) have none. */
function supportsHighlightApi(): boolean {
  return typeof CSS !== "undefined" && "highlights" in CSS;
}

/** A position near `element`'s own bottom-right corner -- the fallback when no precise Range exists. */
function boundingPosition(element: Element): SelectionPosition {
  const rect = element.getBoundingClientRect();
  return { top: rect.bottom, left: rect.right };
}

/** An open note anchored into one or more rendered blocks, with its precise Range if one resolved. */
interface AnchoredNote {
  note: NoteListingEntry;
  start: number;
  end: number;
  range: Range | null;
  blocks: HTMLElement[];
}

/**
 * Which of `entries` (all anchored into the block that was just clicked) the click meant:
 * the one covering the source offset a plain click leaves the browser's own selection
 * collapsed at, or -- when nothing usable resolves (no selection, an offset no entry
 * covers) -- the first one, same as when the block carried only a single note.
 */
function resolveClickedEntry(
  entries: AnchoredNote[],
  container: HTMLElement,
  source: string,
): AnchoredNote {
  if (entries.length === 1) return entries[0];
  const selection = window.getSelection();
  const offset =
    selection?.anchorNode !== null && selection?.anchorNode !== undefined
      ? sourceOffsetOfDomPoint(container, selection.anchorNode, selection.anchorOffset, source)
      : null;
  const resolved = offset === null ? null : narrowestCoveringSpan(entries, offset);
  return resolved ?? entries[0];
}

type StationTab = "files" | "skills" | "notes";
type ViewMode = "read" | "edit";

const TABS: Array<{ id: StationTab; label: string }> = [
  { id: "files", label: "Files" },
  { id: "skills", label: "Skills" },
  { id: "notes", label: "Notes" },
];

interface PendingSpan {
  start: number;
  end: number;
  position: SelectionPosition;
}

export interface StationProps {
  projectId: string;
}

interface ThemedMarkdownViewProps
  extends Pick<MarkdownViewProps, "source" | "resolveWikilink" | "onSelectSpan"> {
  /** Set to the element wrapping the rendered markdown, for measuring against its source positions. */
  containerRef: RefObject<HTMLDivElement | null>;
  /** Fired with a project-relative path when a resolved wikilink is clicked. */
  onFollowWikilink: (path: string) => void;
}

// MarkdownView owns its own container and accepts no theme prop, so the only
// way to carry the app's dark-ness onto it (without editing that component) is
// imperatively: find the rendered `.projects-prose` element after each commit
// and stamp or clear its `data-theme` attribute, which is what its stylesheet
// keys its dark palette to. Clicks on resolved wikilinks are caught the same
// way -- delegated from this wrapper, since the anchors are rendered ones.
function ThemedMarkdownView({
  containerRef,
  source,
  resolveWikilink,
  onSelectSpan,
  onFollowWikilink,
}: ThemedMarkdownViewProps) {
  const isDark = useAppIsDark();

  useEffect(() => {
    stampProseTheme(containerRef.current?.querySelector<HTMLElement>(".projects-prose"), isDark);
  }, [containerRef, isDark]);

  function handleClick(event: MouseEvent<HTMLDivElement>): void {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const anchor = target.closest("a.wikilink");
    if (!anchor) return;
    const path = pathFromWikilinkHref(anchor.getAttribute("href"));
    if (path === null) return;
    event.preventDefault();
    onFollowWikilink(path);
  }

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: the delegated targets are anchors, which dispatch this same click event on Enter.
    // biome-ignore lint/a11y/noStaticElementInteractions: click handling is delegated from wikilink anchors rendered inside, not this wrapper.
    <div ref={containerRef} className="h-full" onClick={handleClick}>
      <MarkdownView source={source} resolveWikilink={resolveWikilink} onSelectSpan={onSelectSpan} />
    </div>
  );
}

interface OpenSkillTarget {
  library: string;
  name: string;
}

// The station shell: a left rail (Files / Skills / Notes) over a center pane.
// Files lists the tree; clicking a markdown entry fetches it and renders it in
// the center pane, with a margin of note dots beside it. Edit swaps the pane
// for a CodeMirror buffer on the same file; Read swaps back, asking first when
// that buffer has unsaved edits. Skills and Notes each host their own
// browser/panel; Skills additionally hands over a builtin skill's own card,
// which opens read-only through its own body query rather than the file path.
// Deleting the file currently open in the center pane clears the pane;
// renaming it follows the selection to its new path.
export function Station({ projectId }: StationProps) {
  const queryClient = useQueryClient();
  const [railOpen, setRailOpen] = useStoredFlag("rail:project", true);
  const [activeTab, setActiveTab] = useState<StationTab>("files");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [openSkill, setOpenSkill] = useState<OpenSkillTarget | null>(null);
  const [mode, setMode] = useState<ViewMode>("read");
  const [selectionAffordance, setSelectionAffordance] = useState<PendingSpan | null>(null);
  const [notePopover, setNotePopover] = useState<PendingSpan | null>(null);
  const [dirty, setDirty] = useState(false);
  const [confirmingDiscard, setConfirmingDiscard] = useState(false);
  const [pendingLine, setPendingLine] = useState<number | null>(null);
  const [lineTops, setLineTops] = useState<Map<number, number>>(new Map());
  const [highlightRequestId, setHighlightRequestId] = useState<string | null>(null);
  // Set by the Notes tab's onOpen -- held here until the just-opened file's own notes
  // query resolves and actually contains this id, then handed to the margin's requestOpen.
  const [pendingNoteId, setPendingNoteId] = useState<string | null>(null);
  // A click on a note's precise anchored span opens its thread right there, near the
  // span's end -- independent of the margin dot, which keeps working on its own.
  const [openThread, setOpenThread] = useState<{
    noteId: string;
    position: SelectionPosition;
  } | null>(null);
  const discardActionRef = useRef<(() => void) | null>(null);
  const proseRef = useRef<HTMLDivElement>(null);

  function openPath(path: string, line: number | null = null, noteId: string | null = null): void {
    setSelectedPath(path);
    setOpenSkill(null);
    setSelectionAffordance(null);
    setNotePopover(null);
    setPendingLine(line);
    setDirty(false);
    setHighlightRequestId(null);
    setPendingNoteId(noteId);
    setOpenThread(null);
  }

  // A builtin skill's file lives outside the project root, so it opens through its own
  // read-only body query rather than the file-editing path; every other skill is an
  // ordinary project file and opens exactly like one, in edit mode.
  function openSkillCard(skill: SkillCard): void {
    if (skill.library === "builtin") {
      setSelectedPath(null);
      setSelectionAffordance(null);
      setNotePopover(null);
      setPendingLine(null);
      setDirty(false);
      setHighlightRequestId(null);
      setPendingNoteId(null);
      setOpenThread(null);
      setOpenSkill({ library: skill.library, name: skill.name });
      setMode("read");
      return;
    }
    setOpenSkill(null);
    openPathForEditing(skill.path);
  }

  // The "Add note" affordance tracks a live selection: once it collapses -- the owner
  // clicked elsewhere, or just cleared it -- the affordance disappears. The open composer
  // is a separate, deliberate state and outlives this; it closes only via Save or Cancel.
  useEffect(() => {
    function handleSelectionChange(): void {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed) {
        setSelectionAffordance(null);
      }
    }
    document.addEventListener("selectionchange", handleSelectionChange);
    return () => document.removeEventListener("selectionchange", handleSelectionChange);
  }, []);

  function openPathForEditing(path: string): void {
    openPath(path);
    setMode("edit");
  }

  // Anything that would throw away the editor's buffer goes through here: with
  // unsaved edits it becomes a confirmation, otherwise it just happens.
  function guardUnsaved(action: () => void): void {
    if (mode !== "edit" || !dirty) {
      action();
      return;
    }
    discardActionRef.current = action;
    setConfirmingDiscard(true);
  }

  const projectsQuery = useQuery({
    queryKey: projectKeys.all,
    queryFn: () => projectsApi.list(),
  });
  const rootPath = projectsQuery.data?.find((entry) => entry.id === projectId)?.root_path ?? null;

  // The same cache entry the file tree fills; the station reads it only to
  // resolve wikilinks against the project's own files.
  const filesQuery = useQuery({
    queryKey: projectKeys.files(projectId),
    queryFn: () => projectsApi.files(projectId),
  });
  const entries = filesQuery.data;
  const resolveWikilink = useMemo(
    () => (target: string) => wikilinkHref(resolveWikilinkTarget(entries ?? [], target)),
    [entries],
  );

  const fileKey = projectKeys.file(projectId, selectedPath ?? "");
  const fileQuery = useQuery({
    queryKey: fileKey,
    queryFn: () => {
      if (selectedPath === null) {
        throw new Error("file query ran with no path selected");
      }
      return projectsApi.readFile(projectId, selectedPath);
    },
    enabled: selectedPath !== null,
  });

  const notesQuery = useQuery({
    queryKey: projectKeys.notes(projectId, selectedPath ?? ""),
    queryFn: () => {
      if (selectedPath === null) {
        throw new Error("notes query ran with no path selected");
      }
      return projectsApi.notes(projectId, selectedPath);
    },
    enabled: selectedPath !== null,
  });

  const skillBodyKey = projectKeys.skillBody(
    projectId,
    openSkill?.library ?? "",
    openSkill?.name ?? "",
  );
  const skillBodyQuery = useQuery({
    queryKey: skillBodyKey,
    queryFn: () => {
      if (openSkill === null) {
        throw new Error("skill body query ran with nothing selected");
      }
      return projectsApi.skillBody(projectId, openSkill.library, openSkill.name);
    },
    enabled: openSkill !== null,
  });

  const source = fileQuery.data?.content;
  const notes = notesQuery.data;
  // Re-derived from the live listing on every render, never a frozen snapshot -- so a
  // resolve/reply/delete inside the open popover is reflected immediately, and the popover
  // simply stops rendering once its note is gone.
  const openThreadNote = openThread
    ? (notes?.find((entry) => entry.id === openThread.noteId) ?? null)
    : null;

  // Every note's dot sits beside the rendered element its line landed in, which
  // is only knowable after that element is laid out -- so it is measured here,
  // once per content or listing change, and handed to the margin as numbers.
  useEffect(() => {
    const container = proseRef.current;
    if (!container || source === undefined || notes === undefined || !hasLayout(container)) {
      setLineTops(new Map());
      return;
    }
    const containerTop = container.getBoundingClientRect().top;
    const measured = new Map<number, number>();
    for (const note of notes) {
      // An orphaned note resolves to no line, so there is no element to measure it against.
      if (note.line === null) continue;
      const element = elementCoveringOffset(container, lineStartOffset(source, note.line));
      if (element) measured.set(note.line, element.getBoundingClientRect().top - containerTop);
    }
    setLineTops(measured);
  }, [source, notes]);

  // An open thread's exact anchored span gets a persistent highlight, clickable to open a
  // thread popover right there. Painted over precisely the anchored characters via the CSS
  // Custom Highlight API when the browser has one; otherwise (jsdom included) the whole
  // covering block gets the old block-level `.note-highlight` look instead. Either way, the
  // covering block(s) carry the click and the pointer-cursor affordance, never narrower --
  // a resolved or archived note has nothing rendered here already (the margin drops it the
  // same way), so only open ones are considered.
  //
  // Two open notes can anchor into the very same block (a shared paragraph, say). Each still
  // gets its own class and its own entry in the Highlight, but the block itself gets exactly
  // one click listener -- registering one per note on a shared element would fire every one
  // of them on a single click, and the last note in the list would always win regardless of
  // which note's text was actually clicked. When a block carries more than one open note,
  // the listener tells them apart the same way a real click leaves the browser's own
  // selection: collapsed right at the clicked character, mapped back to a source offset and
  // matched against each note's own span.
  useEffect(() => {
    const container = proseRef.current;
    if (!container || source === undefined || notes === undefined) return;
    const cleanups: Array<() => void> = [];
    const capable = supportsHighlightApi();
    const highlight = capable ? new Highlight() : null;

    const anchored: AnchoredNote[] = [];
    for (const note of notes) {
      if (note.archived || note.status !== "open" || note.start === null || note.end === null)
        continue;
      const blocks = blocksForSourceSpan(container, note.start, note.end);
      if (blocks.length === 0) continue;
      const range = rangeForSourceSpan(container, note.start, note.end, source);
      anchored.push({ note, start: note.start, end: note.end, range, blocks });
      const cssClass = capable && range !== null ? "note-anchor-hit" : "note-highlight";
      if (capable && range !== null && highlight) highlight.add(range);
      for (const block of blocks) block.classList.add(cssClass);
      cleanups.push(() => {
        for (const block of blocks) block.classList.remove(cssClass);
      });
    }

    const entriesByBlock = new Map<HTMLElement, AnchoredNote[]>();
    for (const entry of anchored) {
      for (const block of entry.blocks) {
        const existing = entriesByBlock.get(block);
        if (existing) existing.push(entry);
        else entriesByBlock.set(block, [entry]);
      }
    }
    for (const [block, entries] of entriesByBlock) {
      const handleClick = () => {
        const chosen = resolveClickedEntry(entries, container, source);
        const position =
          (chosen.range && rangeEndPosition(chosen.range)) ??
          boundingPosition(chosen.blocks[chosen.blocks.length - 1]);
        setOpenThread({ noteId: chosen.note.id, position });
      };
      block.addEventListener("click", handleClick);
      cleanups.push(() => block.removeEventListener("click", handleClick));
    }

    if (capable && highlight) {
      CSS.highlights.set(NOTE_HIGHLIGHT_NAME, highlight);
      cleanups.push(() => CSS.highlights.delete(NOTE_HIGHLIGHT_NAME));
    }
    return () => {
      for (const cleanup of cleanups) cleanup();
    };
  }, [source, notes]);

  // The Notes tab's onOpen hands over a note id before this file's own notes query has
  // necessarily loaded (a fresh file selection remounts that query) -- so the id waits
  // here until the listing for THIS path actually contains it, then is handed to the
  // margin the same way a highlighted-span click would, via requestOpen. Once that
  // listing has settled either way -- loaded without the id (a stale listing, a note
  // deleted in the meantime) or failed outright -- the wait ends regardless, so a vanished
  // id never wedges this state until some unrelated future openPath clears it.
  useEffect(() => {
    if (pendingNoteId === null) return;
    if (notes !== undefined) {
      if (notes.some((note) => note.id === pendingNoteId)) {
        setHighlightRequestId(pendingNoteId);
      }
      setPendingNoteId(null);
      return;
    }
    if (notesQuery.isError) {
      setPendingNoteId(null);
    }
  }, [pendingNoteId, notes, notesQuery.isError]);

  // Opening a file's notes is how a viewer "sees" its agent activity: whatever the
  // latest agent reply is at that moment gets recorded per-viewer, so the tree's
  // note-status dot reads settled until a newer one lands.
  useEffect(() => {
    if (selectedPath === null || notes === undefined) return;
    const agentReplyTimes = notes
      .flatMap((note) => note.replies)
      .filter((reply) => reply.author === "agent")
      .map((reply) => reply.at);
    if (agentReplyTimes.length === 0) return;
    const latest = agentReplyTimes.reduce((max, current) =>
      Date.parse(current) > Date.parse(max) ? current : max,
    );
    markSeen(projectId, selectedPath, latest);
  }, [projectId, selectedPath, notes]);

  // A jump carries a line, which can only be honored once that file's content
  // is on screen -- so it waits here for the render that puts it there.
  useEffect(() => {
    if (pendingLine === null) return;
    const container = proseRef.current;
    if (!container || source === undefined) return;
    const element = elementCoveringOffset(container, lineStartOffset(source, pendingLine));
    setPendingLine(null);
    if (element && typeof element.scrollIntoView === "function") {
      element.scrollIntoView({ block: "start" });
    }
  }, [pendingLine, source]);

  function refreshNotes(): void {
    // The project-wide key is the prefix of every per-file one, so this reaches
    // the Notes panel's listing and the open file's margin alike.
    queryClient.invalidateQueries({ queryKey: projectKeys.notes(projectId) });
  }

  return (
    <div className="flex h-full min-h-0 gap-4">
      <Rail
        side="left"
        title="Project"
        storageKey="rail:project"
        open={railOpen}
        onToggle={setRailOpen}
        defaultWidth={288}
      >
        <div className="flex h-full flex-col gap-2 p-2">
          <div role="tablist" aria-label="Project sections" className="flex gap-1">
            {TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={activeTab === tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  "rounded-md px-2.5 py-1 text-caption",
                  activeTab === tab.id
                    ? "bg-accent-soft font-medium text-accent"
                    : "text-label-2 hover:bg-well hover:text-label-1",
                )}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <div className="min-h-0 flex-1">
            {activeTab === "files" ? (
              <FileTree
                projectId={projectId}
                onSelectFile={(path) => guardUnsaved(() => openPath(path))}
                selectedPath={selectedPath}
                onPathRemoved={(path) => {
                  // The open file is gone: nothing is left to save, so no guard.
                  setSelectedPath((current) => (current === path ? null : current));
                  if (path === selectedPath) setDirty(false);
                }}
                onPathRenamed={(from, to) => {
                  setSelectedPath((current) => (current === from ? to : current));
                }}
              />
            ) : null}
            {activeTab === "skills" ? (
              <SkillsBrowser
                projectId={projectId}
                onOpen={(skill) => guardUnsaved(() => openSkillCard(skill))}
              />
            ) : null}
            {activeTab === "notes" ? (
              <NotesPanel
                projectId={projectId}
                onOpen={(path, line, noteId) => guardUnsaved(() => openPath(path, line, noteId))}
              />
            ) : null}
          </div>
        </div>
      </Rail>

      <section className="relative min-h-0 flex-1 overflow-hidden rounded-lg border border-line bg-surface">
        {selectedPath === null && openSkill === null ? (
          <EmptyState
            className="h-full"
            icon={<FileText size={18} strokeWidth={1.5} />}
            title="Select a file"
            hint="Pick a markdown file from the tree to read it here."
          />
        ) : null}

        {openSkill !== null && skillBodyQuery.isPending ? (
          <p className="p-4 text-caption text-label-3">Loading {openSkill.name}...</p>
        ) : null}

        {openSkill !== null && skillBodyQuery.isError ? (
          <p
            role="alert"
            className="m-4 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
          >
            {studioErrorMessage(skillBodyQuery.error)}
          </p>
        ) : null}

        {openSkill !== null && skillBodyQuery.data ? (
          <div className="flex h-full flex-col">
            <div className="flex items-center justify-between border-line border-b px-3 py-2">
              <span className="truncate text-caption text-label-3">{openSkill.name}</span>
            </div>
            <p className="border-line border-b bg-well px-3 py-1.5 text-caption text-label-2">
              This is a builtin skill. It ships with the app and cannot be edited here.
            </p>
            <div className="min-h-0 flex-1 overflow-auto">
              <MarkdownView source={skillBodyQuery.data.content} />
            </div>
          </div>
        ) : null}

        {selectedPath !== null && fileQuery.isPending ? (
          <p className="p-4 text-caption text-label-3">Loading {selectedPath}...</p>
        ) : null}

        {selectedPath !== null && fileQuery.isError ? (
          <p
            role="alert"
            className="m-4 rounded-md bg-danger-soft px-2.5 py-1.5 text-caption text-danger-ink"
          >
            {studioErrorMessage(fileQuery.error)}
          </p>
        ) : null}

        {selectedPath !== null && mode === "edit" && fileQuery.data ? (
          <MarkdownEditor
            key={selectedPath}
            projectId={projectId}
            path={selectedPath}
            initialContent={fileQuery.data.content}
            hash={fileQuery.data.hash}
            obsidian={fileQuery.data.obsidian}
            absolutePath={rootPath === null ? "" : `${rootPath.replace(/\/$/, "")}/${selectedPath}`}
            onDirtyChange={setDirty}
            onSaved={(newHash) => {
              queryClient.setQueryData<FileContent>(fileKey, (previous) =>
                previous ? { ...previous, hash: newHash } : previous,
              );
              queryClient.invalidateQueries({ queryKey: fileKey });
            }}
            onToggleView={() => guardUnsaved(() => setMode("read"))}
          />
        ) : null}

        {selectedPath !== null && mode === "read" && fileQuery.data ? (
          <div className="flex h-full flex-col">
            <div className="flex items-center justify-between border-line border-b px-3 py-2">
              <span className="truncate text-caption text-label-3">{selectedPath}</span>
              <Button variant="secondary" onClick={() => setMode("edit")}>
                Edit
              </Button>
            </div>

            <div data-testid="reading-scroll" className="min-h-0 flex-1 overflow-auto">
              <div className="projects-prose-row min-h-full">
                <div className="min-w-0 flex-1">
                  <ThemedMarkdownView
                    containerRef={proseRef}
                    source={fileQuery.data.content}
                    resolveWikilink={resolveWikilink}
                    onFollowWikilink={(path) => guardUnsaved(() => openPath(path))}
                    onSelectSpan={(start, end, position) =>
                      setSelectionAffordance({ start, end, position })
                    }
                  />
                </div>

                <div className="w-6 shrink-0 border-line border-l">
                  <NoteMargin
                    projectId={projectId}
                    path={selectedPath}
                    notes={notesQuery.data ?? []}
                    topFor={(line) => lineTops.get(line) ?? null}
                    onChanged={refreshNotes}
                    requestOpen={highlightRequestId}
                    onRequestOpenHandled={() => setHighlightRequestId(null)}
                  />
                </div>
              </div>
            </div>

            {selectionAffordance && !notePopover ? (
              <AddNoteButton
                position={selectionAffordance.position}
                onClick={() => {
                  setNotePopover(selectionAffordance);
                  setSelectionAffordance(null);
                }}
              />
            ) : null}

            {notePopover ? (
              <NotePopover
                projectId={projectId}
                path={selectedPath}
                source={fileQuery.data.content}
                start={notePopover.start}
                end={notePopover.end}
                position={notePopover.position}
                onSaved={() => {
                  setNotePopover(null);
                  refreshNotes();
                }}
                onCancel={() => setNotePopover(null)}
              />
            ) : null}

            {openThread && openThreadNote ? (
              <ThreadPopover
                projectId={projectId}
                path={selectedPath}
                note={openThreadNote}
                position={openThread.position}
                onChanged={refreshNotes}
                onClose={() => setOpenThread(null)}
              />
            ) : null}
          </div>
        ) : null}
      </section>

      <ConfirmDialog
        open={confirmingDiscard}
        title="Discard unsaved changes?"
        message="This file has edits that were never saved. Leaving now loses them."
        confirmLabel="Discard"
        destructive
        onConfirm={() => {
          const action = discardActionRef.current;
          discardActionRef.current = null;
          setConfirmingDiscard(false);
          setDirty(false);
          action?.();
        }}
        onCancel={() => {
          discardActionRef.current = null;
          setConfirmingDiscard(false);
        }}
      />
    </div>
  );
}
