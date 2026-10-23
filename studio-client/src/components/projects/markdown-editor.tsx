"use client";

import { markdown } from "@codemirror/lang-markdown";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { basicSetup } from "codemirror";
import type { KeyboardEvent } from "react";
import { useEffect, useRef, useState } from "react";

import { ApiError, type FileContent, projectsApi } from "@/api/projects";
import { studioErrorMessage } from "@/api/studio";
import { Button } from "@/shared/ui/button";

import { stampProseTheme, useAppIsDark } from "./use-app-dark";
import "./prose.css";

export interface MarkdownEditorProps {
  /** The project the file belongs to; every read and write here goes to this one. */
  projectId: string;
  path: string;
  initialContent: string;
  hash: string;
  obsidian: boolean;
  absolutePath: string;
  onSaved: (newHash: string) => void;
  onToggleView: () => void;
  /** Fired on mount and whenever the buffer starts or stops differing from the last saved content. */
  onDirtyChange?: (dirty: boolean) => void;
}

// Reuses the reading view's tokens (defined under .projects-prose in prose.css)
// so the editor and the rendered view read as one surface.
const EDITOR_THEME = EditorView.theme({
  "&": {
    height: "100%",
    color: "var(--prose-fg)",
    backgroundColor: "var(--prose-bg)",
  },
  ".cm-content": {
    fontFamily: "var(--prose-font-mono)",
    fontSize: "0.95em",
    caretColor: "var(--prose-fg)",
  },
  ".cm-gutters": {
    backgroundColor: "var(--prose-bg-well)",
    color: "var(--prose-fg-faint)",
    borderRight: "1px solid var(--prose-border)",
  },
  ".cm-activeLine, .cm-activeLineGutter": {
    backgroundColor: "var(--prose-bg-well)",
  },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
    backgroundColor: "var(--prose-selection) !important",
  },
  ".cm-scroller": {
    overflow: "auto",
  },
});

// Hosts a CodeMirror 6 buffer for one file. A path/hash change is a different
// file, not an edit of this one, so the caller remounts (key={path}) rather
// than this component re-syncing CM6's document from new props after mount.
export function MarkdownEditor({
  projectId,
  path,
  initialContent,
  hash,
  obsidian,
  absolutePath,
  onSaved,
  onToggleView,
  onDirtyChange,
}: MarkdownEditorProps) {
  const isDark = useAppIsDark();
  const rootRef = useRef<HTMLDivElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const hashRef = useRef(hash);
  const savedContentRef = useRef(initialContent);
  const dirtyRef = useRef(false);
  const onDirtyChangeRef = useRef(onDirtyChange);
  onDirtyChangeRef.current = onDirtyChange;

  const [conflict, setConflict] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);

  useEffect(() => {
    stampProseTheme(rootRef.current, isDark);
  }, [isDark]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: mounts CM6 once from the initial props; a different file arrives as a remount (key={path}), not a prop update.
  useEffect(() => {
    if (!hostRef.current) return;
    function reportDirty(next: boolean): void {
      if (dirtyRef.current === next) return;
      dirtyRef.current = next;
      onDirtyChangeRef.current?.(next);
    }
    const view = new EditorView({
      state: EditorState.create({
        doc: initialContent,
        extensions: [
          basicSetup,
          markdown(),
          EDITOR_THEME,
          EditorView.updateListener.of((update) => {
            if (!update.docChanged) return;
            reportDirty(update.state.doc.toString() !== savedContentRef.current);
          }),
        ],
      }),
      parent: hostRef.current,
    });
    viewRef.current = view;
    dirtyRef.current = false;
    onDirtyChangeRef.current?.(false);
    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, []);

  function markSaved(content: string): void {
    savedContentRef.current = content;
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    onDirtyChangeRef.current?.(false);
  }

  function replaceBuffer(content: string): void {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: content },
    });
  }

  async function save(): Promise<void> {
    const view = viewRef.current;
    if (!view) return;
    const content = view.state.doc.toString();

    setSaving(true);
    setSaveError(null);
    try {
      const newHash = await projectsApi.writeFile(projectId, path, content, hashRef.current);
      hashRef.current = newHash;
      setConflict(false);
      markSaved(content);
      onSaved(newHash);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setConflict(true);
      } else {
        setSaveError(studioErrorMessage(error));
      }
    } finally {
      setSaving(false);
    }
  }

  async function reload(): Promise<void> {
    setReloading(true);
    try {
      const file: FileContent = await projectsApi.readFile(projectId, path);
      markSaved(file.content);
      replaceBuffer(file.content);
      hashRef.current = file.hash;
      setConflict(false);
      setSaveError(null);
    } catch (error) {
      setSaveError(studioErrorMessage(error));
    } finally {
      setReloading(false);
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const modifierPressed = event.metaKey || event.ctrlKey;
    if (modifierPressed && event.key === "s") {
      event.preventDefault();
      void save();
    }
  }

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: captures the Cmd/Ctrl+S save shortcut anywhere in the editor subtree, not a click target.
    <div
      ref={rootRef}
      data-testid="markdown-editor"
      onKeyDown={handleKeyDown}
      className="projects-prose flex h-full flex-col"
      style={{ maxWidth: "none", margin: 0, padding: 0 }}
    >
      <div className="flex items-center justify-between gap-2 border-line border-b px-3 py-2">
        <div className="flex items-center gap-3">
          <Button variant="secondary" onClick={onToggleView}>
            Read
          </Button>
          {obsidian ? (
            <a
              href={`obsidian://open?path=${encodeURIComponent(absolutePath)}`}
              className="text-caption text-label-2 underline"
            >
              Open in Obsidian
            </a>
          ) : null}
        </div>
        <Button onClick={() => void save()} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
      </div>

      {conflict ? (
        <div
          role="alert"
          className="flex items-center justify-between gap-2 border-warn-soft border-b bg-warn-soft px-3 py-2 text-caption text-warn-ink"
        >
          <span>This file changed on disk. Reload to see the latest version?</span>
          <Button variant="secondary" onClick={() => void reload()} disabled={reloading}>
            {reloading ? "Reloading…" : "Reload"}
          </Button>
        </div>
      ) : null}

      {saveError && !conflict ? (
        <p
          role="alert"
          className="border-danger-soft border-b bg-danger-soft px-3 py-2 text-caption text-danger-ink"
        >
          {saveError}
        </p>
      ) : null}

      <div ref={hostRef} className="min-h-0 flex-1 overflow-auto" />
    </div>
  );
}
