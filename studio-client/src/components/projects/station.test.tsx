import { EditorView } from "@codemirror/view";
import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  FileContent,
  FileEntry,
  NoteListingEntry,
  ProjectListingEntry,
  SkillCard,
} from "@/api/projects";
import { render } from "@/test-support/render";

vi.mock("@/api/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/projects")>();
  return {
    projectKeys: actual.projectKeys,
    projectsApi: {
      list: vi.fn(),
      files: vi.fn(),
      readFile: vi.fn(),
      createFile: vi.fn(),
      createDir: vi.fn(),
      writeFile: vi.fn(),
      renameFile: vi.fn(),
      deleteFile: vi.fn(),
      notes: vi.fn(),
      notesSummary: vi.fn(),
      createNote: vi.fn(),
      updateNote: vi.fn(),
      updateReply: vi.fn(),
      replyToNote: vi.fn(),
      resolveNote: vi.fn(),
      reopenNote: vi.fn(),
      deleteNote: vi.fn(),
      skills: vi.fn(),
      installSkill: vi.fn(),
      skillBody: vi.fn(),
    },
  };
});

// Imported after the mock so the component sees the mocked module.
import { projectsApi } from "@/api/projects";

import { Station } from "./station";

// CM6 measures the viewport on mount. jsdom implements neither Range
// measurement nor element layout, so a bare mount throws without these
// stand-ins -- scoped to this file only, never inside the component.
function stubLayoutMeasurement(): void {
  const rect: DOMRect = {
    x: 0,
    y: 0,
    width: 0,
    height: 0,
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    toJSON: () => ({}),
  };
  const rectList = {
    item: () => rect,
    length: 1,
    0: rect,
    [Symbol.iterator]: function* () {
      yield rect;
    } as unknown as () => Iterator<DOMRect>,
  } as unknown as DOMRectList;

  document.createRange = () => {
    const range = new Range();
    range.getBoundingClientRect = () => rect;
    range.getClientRects = () => rectList;
    return range;
  };
  Element.prototype.getBoundingClientRect = () => rect;
  Element.prototype.getClientRects = () => rectList;
}

function renderStation(projectId = "proj-1") {
  return render(<Station projectId={projectId} />);
}

// Rename and Delete live behind the tree row's own dot menu now, not as inline icon
// buttons -- opens it and hands back the (single, generically-labelled) menu item.
async function openRowMenuItem(
  user: ReturnType<typeof userEvent.setup>,
  path: string,
  item: "Rename" | "Delete",
): Promise<void> {
  await user.click(await screen.findByRole("button", { name: `Actions for ${path}` }));
  await user.click(screen.getByRole("menuitem", { name: item }));
}

// Edits the mounted buffer through CodeMirror's own API: jsdom implements no
// contenteditable, so typing into `.cm-content` never reaches the document.
function dirtyTheBuffer(): void {
  const dom = document.querySelector<HTMLElement>(".cm-editor");
  if (!dom) throw new Error("expected a mounted CodeMirror editor");
  const view = EditorView.findFromDOM(dom);
  if (!view) throw new Error("expected a CodeMirror view behind the editor DOM");
  act(() => {
    view.dispatch({ changes: { from: view.state.doc.length, insert: " edited" } });
  });
}

function selectText(paragraph: Element, start: number, end: number): void {
  const textNode = paragraph.firstChild;
  if (!textNode) throw new Error("expected the paragraph to have text content");
  const range = document.createRange();
  range.setStart(textNode, start);
  range.setEnd(textNode, end);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  paragraph.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
}

// A plain click (no drag) leaves the browser's own selection collapsed right at the
// clicked character -- this is what tells apart two notes anchored in the same block.
function clickAtOffset(target: Element, node: Node, offset: number): void {
  const range = document.createRange();
  range.setStart(node, offset);
  range.setEnd(node, offset);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

beforeEach(() => {
  stubLayoutMeasurement();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  delete document.documentElement.dataset.theme;
});

const PROJECTS: ProjectListingEntry[] = [
  { id: "proj-1", name: "Proj One", root_path: "/vault", missing: false, running_sessions: 0 },
];

const ENTRIES: FileEntry[] = [
  { path: "readme.md", kind: "file", size: 10 },
  { path: "image.png", kind: "file", size: 500 },
];

const CONTENT: FileContent = { content: "Hello", hash: "h1", obsidian: false };

function note(overrides: Partial<NoteListingEntry> = {}): NoteListingEntry {
  return {
    id: "note-1",
    anchor: { exact: "Hello", prefix: "", suffix: "" },
    text: "a note",
    created_at: "2026-08-23T00:00:00Z",
    archived: false,
    replies: [],
    status: "open",
    path: "readme.md",
    orphaned: false,
    line: 1,
    start: 0,
    end: 5,
    ...overrides,
  };
}

function mockProject(): void {
  vi.mocked(projectsApi.list).mockResolvedValue(PROJECTS);
  vi.mocked(projectsApi.files).mockResolvedValue(ENTRIES);
  vi.mocked(projectsApi.readFile).mockResolvedValue(CONTENT);
  vi.mocked(projectsApi.notes).mockResolvedValue([]);
  vi.mocked(projectsApi.notesSummary).mockResolvedValue([]);
}

describe("<Station />", () => {
  it("shows the Files tab active by default, with the tree inside it", async () => {
    mockProject();

    renderStation();

    const tablist = screen.getByRole("tablist", { name: "Project sections" });
    expect(within(tablist).getByRole("tab", { name: "Files", selected: true })).toBeInTheDocument();
    expect(await screen.findByRole("tree", { name: "Project files" })).toBeInTheDocument();
  });

  it("shows an empty center pane before any file is selected", () => {
    mockProject();

    renderStation();

    expect(screen.getByText(/select a file/i)).toBeInTheDocument();
  });

  it("loads and renders a clicked markdown file's content in the center pane", async () => {
    mockProject();
    const user = userEvent.setup();

    renderStation();
    await user.click(await screen.findByRole("button", { name: "readme.md" }));

    await waitFor(() => expect(projectsApi.readFile).toHaveBeenCalledWith("proj-1", "readme.md"));
    expect(await screen.findByText("Hello")).toBeInTheDocument();
  });

  it("bridges the app's dark html theme onto the rendered markdown container", async () => {
    mockProject();
    const user = userEvent.setup();
    document.documentElement.dataset.theme = "factory";

    renderStation();
    await user.click(await screen.findByRole("button", { name: "readme.md" }));
    await screen.findByText("Hello");

    const prose = document.querySelector(".projects-prose");
    expect(prose).toHaveAttribute("data-theme", "dark");
  });

  it("removes the dark bridge attribute when the html theme goes back to paper", async () => {
    mockProject();
    const user = userEvent.setup();
    document.documentElement.dataset.theme = "factory";

    renderStation();
    await user.click(await screen.findByRole("button", { name: "readme.md" }));
    await screen.findByText("Hello");
    expect(document.querySelector(".projects-prose")).toHaveAttribute("data-theme", "dark");

    delete document.documentElement.dataset.theme;
    await waitFor(() =>
      expect(document.querySelector(".projects-prose")).not.toHaveAttribute("data-theme"),
    );
  });

  it("clears the center pane after the open file is deleted via the tree", async () => {
    mockProject();
    vi.mocked(projectsApi.deleteFile).mockResolvedValue(undefined);
    const user = userEvent.setup();

    renderStation();
    await user.click(await screen.findByRole("button", { name: "readme.md" }));
    await screen.findByText("Hello");

    await openRowMenuItem(user, "readme.md", "Delete");
    vi.mocked(projectsApi.files).mockResolvedValue([
      { path: "image.png", kind: "file", size: 500 },
    ]);
    await user.click(screen.getByRole("button", { name: "Delete it" }));

    await waitFor(() => expect(projectsApi.deleteFile).toHaveBeenCalledWith("proj-1", "readme.md"));
    expect(await screen.findByText(/select a file/i)).toBeInTheDocument();
    expect(screen.queryByText("Hello")).not.toBeInTheDocument();
  });

  it("follows the open file to its new path after a rename via the tree", async () => {
    mockProject();
    vi.mocked(projectsApi.renameFile).mockResolvedValue(undefined);
    const RENAMED: FileContent = { content: "Renamed", hash: "h2", obsidian: false };
    const user = userEvent.setup();

    renderStation();
    await user.click(await screen.findByRole("button", { name: "readme.md" }));
    await screen.findByText("Hello");

    await openRowMenuItem(user, "readme.md", "Rename");
    const form = screen.getByRole("form", { name: "Rename readme.md" });
    const input = within(form).getByRole("textbox");
    await user.clear(input);
    await user.type(input, "renamed.md");
    vi.mocked(projectsApi.files).mockResolvedValue([
      { path: "renamed.md", kind: "file", size: 10 },
      { path: "image.png", kind: "file", size: 500 },
    ]);
    vi.mocked(projectsApi.readFile).mockImplementation((_projectId, path) =>
      Promise.resolve(path === "renamed.md" ? RENAMED : CONTENT),
    );
    await user.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(projectsApi.renameFile).toHaveBeenCalledWith("proj-1", "readme.md", "renamed.md"),
    );
    expect(await screen.findByText("Renamed")).toBeInTheDocument();
  });

  describe("edit mode", () => {
    it("Edit mounts MarkdownEditor for the selected file, with the absolute path built from root_path", async () => {
      mockProject();
      vi.mocked(projectsApi.readFile).mockResolvedValue({ ...CONTENT, obsidian: true });
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      await screen.findByText("Hello");
      await user.click(screen.getByRole("button", { name: "Edit" }));

      expect(await screen.findByTestId("markdown-editor")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /obsidian/i })).toHaveAttribute(
        "href",
        `obsidian://open?path=${encodeURIComponent("/vault/readme.md")}`,
      );
    });

    it("strips a trailing slash from root_path before joining the file path", async () => {
      mockProject();
      vi.mocked(projectsApi.list).mockResolvedValue([{ ...PROJECTS[0], root_path: "/vault/" }]);
      vi.mocked(projectsApi.readFile).mockResolvedValue({ ...CONTENT, obsidian: true });
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      await screen.findByText("Hello");
      await user.click(screen.getByRole("button", { name: "Edit" }));

      await screen.findByTestId("markdown-editor");
      expect(screen.getByRole("link", { name: /obsidian/i })).toHaveAttribute(
        "href",
        `obsidian://open?path=${encodeURIComponent("/vault/readme.md")}`,
      );
    });

    it("onToggleView (the editor's Read button) returns to reading view", async () => {
      mockProject();
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      await screen.findByText("Hello");
      await user.click(screen.getByRole("button", { name: "Edit" }));
      await screen.findByTestId("markdown-editor");

      await user.click(screen.getByRole("button", { name: "Read" }));

      expect(await screen.findByText("Hello")).toBeInTheDocument();
      expect(screen.queryByTestId("markdown-editor")).not.toBeInTheDocument();
    });

    it("onSaved updates the cached hash and invalidates the file query, so toggling back to read shows the saved content", async () => {
      mockProject();
      vi.mocked(projectsApi.writeFile).mockResolvedValue("h2");
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      await screen.findByText("Hello");
      await user.click(screen.getByRole("button", { name: "Edit" }));
      await screen.findByTestId("markdown-editor");

      vi.mocked(projectsApi.readFile).mockResolvedValue({
        content: "Saved content",
        hash: "h2",
        obsidian: false,
      });
      await user.click(screen.getByRole("button", { name: "Save" }));
      await waitFor(() => expect(projectsApi.writeFile).toHaveBeenCalledTimes(1));

      await user.click(screen.getByRole("button", { name: "Read" }));

      expect(await screen.findByText("Saved content")).toBeInTheDocument();
    });

    it("switching files while editing remounts the editor with the newly selected file's content (key={path})", async () => {
      mockProject();
      vi.mocked(projectsApi.files).mockResolvedValue([
        { path: "a.md", kind: "file", size: 3 },
        { path: "b.md", kind: "file", size: 3 },
      ]);
      vi.mocked(projectsApi.readFile).mockImplementation((_projectId, path) =>
        Promise.resolve(
          path === "a.md"
            ? { content: "content-a", hash: "ha", obsidian: false }
            : { content: "content-b", hash: "hb", obsidian: false },
        ),
      );
      const user = userEvent.setup();

      renderStation();
      // Visit b.md once in reading view first so its file query is already
      // cached -- switching to it later while editing then resolves in the
      // same render pass, the exact case an unkeyed editor would misrender.
      await user.click(await screen.findByRole("button", { name: "b.md" }));
      await screen.findByText("content-b");
      await user.click(screen.getByRole("button", { name: "a.md" }));
      await screen.findByText("content-a");

      await user.click(screen.getByRole("button", { name: "Edit" }));
      await waitFor(() =>
        expect(document.querySelector(".cm-content")?.textContent).toBe("content-a"),
      );

      await user.click(screen.getByRole("button", { name: "b.md" }));

      await waitFor(() =>
        expect(document.querySelector(".cm-content")?.textContent).toBe("content-b"),
      );
    });
  });

  describe("notes", () => {
    it("selecting text shows the Add note affordance, not the composer, at the selection's end", async () => {
      mockProject();
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      const paragraph = (await screen.findByText("Hello")).closest("p");
      if (!paragraph) throw new Error("expected the rendered content in a paragraph");

      selectText(paragraph, 0, 5);

      expect(await screen.findByRole("button", { name: "Add note" })).toBeInTheDocument();
      expect(screen.queryByRole("dialog", { name: "New note" })).not.toBeInTheDocument();
    });

    it("clicking Add note opens the composer at the same spot; saving re-fetches the file's notes", async () => {
      mockProject();
      vi.mocked(projectsApi.createNote).mockResolvedValue({
        id: "note-2",
        anchor: { exact: "Hello", prefix: "", suffix: "" },
        text: "a new note",
        created_at: "2026-08-23T00:00:00Z",
        archived: false,
        replies: [],
        status: "open",
      });
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      const paragraph = (await screen.findByText("Hello")).closest("p");
      if (!paragraph) throw new Error("expected the rendered content in a paragraph");

      await waitFor(() => expect(projectsApi.notes).toHaveBeenCalledTimes(1));
      selectText(paragraph, 0, 5);
      await user.click(await screen.findByRole("button", { name: "Add note" }));

      const dialog = await screen.findByRole("dialog", { name: "New note" });
      expect(screen.queryByRole("button", { name: "Add note" })).not.toBeInTheDocument();
      await user.type(within(dialog).getByRole("textbox"), "a new note");
      await user.click(within(dialog).getByRole("button", { name: "Save" }));

      await waitFor(() =>
        expect(projectsApi.createNote).toHaveBeenCalledWith(
          "proj-1",
          "readme.md",
          { exact: "Hello", prefix: "", suffix: "" },
          "a new note",
        ),
      );
      await waitFor(() => expect(projectsApi.notes).toHaveBeenCalledTimes(2));
      expect(screen.queryByRole("dialog", { name: "New note" })).not.toBeInTheDocument();
    });

    it("clearing the selection dismisses the affordance without opening the composer", async () => {
      mockProject();
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      const paragraph = (await screen.findByText("Hello")).closest("p");
      if (!paragraph) throw new Error("expected the rendered content in a paragraph");

      selectText(paragraph, 0, 5);
      await screen.findByRole("button", { name: "Add note" });

      window.getSelection()?.removeAllRanges();
      document.dispatchEvent(new Event("selectionchange"));

      await waitFor(() =>
        expect(screen.queryByRole("button", { name: "Add note" })).not.toBeInTheDocument(),
      );
    });

    it("shows the open file's notes in the margin and resolving one re-fetches the listing", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([note()]);
      vi.mocked(projectsApi.resolveNote).mockResolvedValue({ ...note(), status: "resolved" });
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      await user.hover(await screen.findByRole("button", { name: "Note on line 1" }));
      await user.click(screen.getByRole("button", { name: "Resolve" }));

      await waitFor(() =>
        expect(projectsApi.resolveNote).toHaveBeenCalledWith("proj-1", "note-1", "readme.md"),
      );
      await waitFor(() => expect(projectsApi.notes).toHaveBeenCalledTimes(2));
    });

    it("keeps the note margin inside the scrolling pane, so the dots scroll with the text", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([note()]);
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));

      const dot = await screen.findByRole("button", { name: "Note on line 1" });
      const scroller = screen.getByTestId("reading-scroll");
      expect(scroller).toContainElement(dot);
      expect(scroller).toContainElement(screen.getByText("Hello"));
    });

    it("refreshes the Notes panel's own listing when a note is resolved from the margin", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([note()]);
      vi.mocked(projectsApi.resolveNote).mockResolvedValue({ ...note(), status: "resolved" });
      const user = userEvent.setup();
      const panelFetches = () =>
        vi.mocked(projectsApi.notes).mock.calls.filter((call) => call.length === 1).length;

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      await screen.findByText("Hello");
      await user.click(screen.getByRole("tab", { name: "Notes" }));
      await waitFor(() => expect(panelFetches()).toBe(1));

      await user.hover(screen.getByRole("button", { name: "Note on line 1" }));
      await user.click(screen.getByRole("button", { name: "Resolve" }));

      await waitFor(() => expect(panelFetches()).toBe(2));
    });

    it("deleting a note from the margin, once confirmed, re-fetches the listing", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([note()]);
      vi.mocked(projectsApi.deleteNote).mockResolvedValue(undefined);
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      await user.hover(await screen.findByRole("button", { name: "Note on line 1" }));
      await user.click(screen.getByRole("button", { name: "Delete" }));
      const dialog = await screen.findByRole("dialog");
      await user.click(within(dialog).getByRole("button", { name: "Delete" }));

      await waitFor(() =>
        expect(projectsApi.deleteNote).toHaveBeenCalledWith("proj-1", "note-1", "readme.md"),
      );
      await waitFor(() => expect(projectsApi.notes).toHaveBeenCalledTimes(2));
    });
  });

  describe("Notes tab", () => {
    it("hosts NotesPanel, and its jump action selects that file in the center pane", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([note({ path: "readme.md", line: 1 })]);
      const user = userEvent.setup();

      renderStation();
      await screen.findByRole("tree", { name: "Project files" });
      await user.click(screen.getByRole("tab", { name: "Notes" }));

      const row = (await screen.findByText("a note")).closest("li");
      if (!row) throw new Error("expected the note to render inside a list item");
      await user.click(within(row).getByRole("button", { name: /jump/i }));

      // Selector-scoped: the note's own opened thread card also quotes "Hello" (the
      // anchored span), so the file's own rendered paragraph is picked out by tag.
      expect(await screen.findByText("Hello", { selector: "p" })).toBeInTheDocument();
    });

    it("clicking a note's own text opens the file AND that note's thread", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([note()]);
      const user = userEvent.setup();

      renderStation();
      await screen.findByRole("tree", { name: "Project files" });
      await user.click(screen.getByRole("tab", { name: "Notes" }));

      await user.click(await screen.findByRole("button", { name: "a note" }));

      expect(await screen.findByText("Hello", { selector: "p" })).toBeInTheDocument();
      expect(await screen.findByRole("note")).toHaveTextContent("a note");
    });

    it("opens the file without a thread popover when the requested note id is stale", async () => {
      mockProject();
      // The Notes tab's own listing (called with no path) still carries a note the file's
      // own per-file listing (called with a path) no longer does -- a stale row, same as one
      // deleted moments earlier.
      vi.mocked(projectsApi.notes).mockImplementation((_projectId, path) =>
        Promise.resolve(path === undefined ? [note({ id: "stale-id" })] : []),
      );
      const user = userEvent.setup();

      renderStation();
      await screen.findByRole("tree", { name: "Project files" });
      await user.click(screen.getByRole("tab", { name: "Notes" }));

      await user.click(await screen.findByRole("button", { name: "a note" }));

      expect(await screen.findByText("Hello", { selector: "p" })).toBeInTheDocument();
      expect(screen.queryByRole("note")).not.toBeInTheDocument();
    });
  });

  describe("unsaved edits", () => {
    async function openAndDirty(user: ReturnType<typeof userEvent.setup>): Promise<void> {
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      await screen.findByText("Hello");
      await user.click(screen.getByRole("button", { name: "Edit" }));
      await screen.findByTestId("markdown-editor");
      dirtyTheBuffer();
    }

    it("asks before leaving a dirty buffer, and stays put on cancel", async () => {
      mockProject();
      const user = userEvent.setup();

      renderStation();
      await openAndDirty(user);
      await user.click(screen.getByRole("button", { name: "Read" }));

      expect(await screen.findByText("Discard unsaved changes?")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Cancel" }));

      expect(screen.queryByText("Discard unsaved changes?")).not.toBeInTheDocument();
      expect(screen.getByTestId("markdown-editor")).toBeInTheDocument();
    });

    it("drops the edits and returns to reading view on confirm", async () => {
      mockProject();
      const user = userEvent.setup();

      renderStation();
      await openAndDirty(user);
      await user.click(screen.getByRole("button", { name: "Read" }));
      await user.click(screen.getByRole("button", { name: "Discard" }));

      expect(await screen.findByText("Hello")).toBeInTheDocument();
      expect(screen.queryByTestId("markdown-editor")).not.toBeInTheDocument();
      expect(projectsApi.writeFile).not.toHaveBeenCalled();
    });

    it("does not ask when the buffer is clean", async () => {
      mockProject();
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      await screen.findByText("Hello");
      await user.click(screen.getByRole("button", { name: "Edit" }));
      await screen.findByTestId("markdown-editor");

      await user.click(screen.getByRole("button", { name: "Read" }));

      expect(screen.queryByText("Discard unsaved changes?")).not.toBeInTheDocument();
      expect(await screen.findByText("Hello")).toBeInTheDocument();
    });

    it("asks before switching files while a buffer is dirty", async () => {
      mockProject();
      vi.mocked(projectsApi.files).mockResolvedValue([
        { path: "readme.md", kind: "file", size: 10 },
        { path: "other.md", kind: "file", size: 10 },
      ]);
      vi.mocked(projectsApi.readFile).mockImplementation((_projectId, path) =>
        Promise.resolve(
          path === "other.md"
            ? { content: "Other file", hash: "h2", obsidian: false }
            : { content: "Hello", hash: "h1", obsidian: false },
        ),
      );
      const user = userEvent.setup();

      renderStation();
      await openAndDirty(user);
      await user.click(screen.getByRole("button", { name: "other.md" }));

      expect(await screen.findByText("Discard unsaved changes?")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Discard" }));

      await waitFor(() =>
        expect(document.querySelector(".cm-content")?.textContent).toBe("Other file"),
      );
    });

    it("asks before a Skills-tab open replaces a dirty buffer", async () => {
      mockProject();
      const SKILL: SkillCard = {
        name: "summarize",
        description: "Summarizes a note.",
        library: "team-lib",
        path: "libs/team-lib/summarize/SKILL.md",
        valid: true,
        error: null,
      };
      vi.mocked(projectsApi.skills).mockResolvedValue([SKILL]);
      vi.mocked(projectsApi.readFile).mockImplementation((_projectId, path) =>
        Promise.resolve(
          path === SKILL.path ? { content: "skill body", hash: "hs", obsidian: false } : CONTENT,
        ),
      );
      const user = userEvent.setup();

      renderStation();
      await openAndDirty(user);
      await user.click(screen.getByRole("tab", { name: "Skills" }));
      await user.click(await screen.findByRole("button", { name: "Open summarize" }));

      expect(await screen.findByText("Discard unsaved changes?")).toBeInTheDocument();
    });

    it("skips the guard when the dirty file is deleted out from under the editor", async () => {
      mockProject();
      vi.mocked(projectsApi.deleteFile).mockResolvedValue(undefined);
      const user = userEvent.setup();

      renderStation();
      await openAndDirty(user);

      await openRowMenuItem(user, "readme.md", "Delete");
      vi.mocked(projectsApi.files).mockResolvedValue([
        { path: "image.png", kind: "file", size: 500 },
      ]);
      await user.click(screen.getByRole("button", { name: "Delete it" }));

      expect(await screen.findByText(/select a file/i)).toBeInTheDocument();
      expect(screen.queryByText("Discard unsaved changes?")).not.toBeInTheDocument();
    });

    it("keeps guarding a dirty buffer after some other file is deleted", async () => {
      mockProject();
      vi.mocked(projectsApi.files).mockResolvedValue([
        { path: "readme.md", kind: "file", size: 10 },
        { path: "other.md", kind: "file", size: 10 },
      ]);
      vi.mocked(projectsApi.deleteFile).mockResolvedValue(undefined);
      const user = userEvent.setup();

      renderStation();
      await openAndDirty(user);

      await openRowMenuItem(user, "other.md", "Delete");
      await user.click(screen.getByRole("button", { name: "Delete it" }));
      await waitFor(() =>
        expect(projectsApi.deleteFile).toHaveBeenCalledWith("proj-1", "other.md"),
      );

      await user.click(screen.getByRole("button", { name: "Read" }));

      expect(await screen.findByText("Discard unsaved changes?")).toBeInTheDocument();
    });
  });

  describe("wikilinks", () => {
    const LINKED: FileEntry[] = [
      { path: "readme.md", kind: "file", size: 10 },
      { path: "notes", kind: "dir", size: 0 },
      { path: "notes/Home.md", kind: "file", size: 10 },
    ];

    function mockLinkedProject(body: string): void {
      mockProject();
      vi.mocked(projectsApi.files).mockResolvedValue(LINKED);
      vi.mocked(projectsApi.readFile).mockImplementation((_projectId, path) =>
        Promise.resolve(
          path === "notes/Home.md"
            ? { content: "The home page", hash: "h2", obsidian: false }
            : { content: body, hash: "h1", obsidian: false },
        ),
      );
    }

    it("opens the file a resolved wikilink points at", async () => {
      mockLinkedProject("See [[Home]] for more.");
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));

      await user.click(await screen.findByRole("link", { name: "Home" }));

      await waitFor(() =>
        expect(projectsApi.readFile).toHaveBeenCalledWith("proj-1", "notes/Home.md"),
      );
      expect(await screen.findByText("The home page")).toBeInTheDocument();
    });

    it("renders a wikilink to nothing in the project as a dead link", async () => {
      mockLinkedProject("See [[Nowhere]] for more.");
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));

      const dead = await screen.findByText("Nowhere");
      expect(dead.tagName).toBe("SPAN");
      expect(screen.queryByRole("link", { name: "Nowhere" })).not.toBeInTheDocument();
    });
  });

  describe("Skills tab", () => {
    it("hosts SkillsBrowser, and its Open action selects that file and enters edit mode", async () => {
      mockProject();
      const SKILL: SkillCard = {
        name: "summarize",
        description: "Summarizes a note.",
        library: "team-lib",
        path: "libs/team-lib/summarize/SKILL.md",
        valid: true,
        error: null,
      };
      vi.mocked(projectsApi.skills).mockResolvedValue([SKILL]);
      vi.mocked(projectsApi.readFile).mockImplementation((_projectId, path) =>
        Promise.resolve(
          path === SKILL.path ? { content: "skill body", hash: "hs", obsidian: false } : CONTENT,
        ),
      );
      const user = userEvent.setup();

      renderStation();
      await screen.findByRole("tree", { name: "Project files" });
      await user.click(screen.getByRole("tab", { name: "Skills" }));

      await user.click(await screen.findByRole("button", { name: "Open summarize" }));

      expect(await screen.findByTestId("markdown-editor")).toBeInTheDocument();
      await waitFor(() =>
        expect(document.querySelector(".cm-content")?.textContent).toBe("skill body"),
      );
    });

    it("opens a builtin skill's full body read-only, through its own body endpoint", async () => {
      mockProject();
      const BUILTIN: SkillCard = {
        name: "core-search",
        description: "Ships with the app.",
        library: "builtin",
        path: "/opt/harness/skills/core-search/SKILL.md",
        valid: true,
        error: null,
      };
      vi.mocked(projectsApi.skills).mockResolvedValue([BUILTIN]);
      vi.mocked(projectsApi.skillBody).mockResolvedValue({
        content: "# Core search\n\nFull builtin body.",
        hash: "hb1",
        editable: false,
      });
      const user = userEvent.setup();

      renderStation();
      await screen.findByRole("tree", { name: "Project files" });
      await user.click(screen.getByRole("tab", { name: "Skills" }));
      await user.click(await screen.findByRole("button", { name: "Open core-search" }));

      expect(await screen.findByText("Full builtin body.")).toBeInTheDocument();
      expect(projectsApi.skillBody).toHaveBeenCalledWith("proj-1", "builtin", "core-search");
      expect(screen.queryByTestId("markdown-editor")).not.toBeInTheDocument();
      expect(screen.getByText(/cannot be edited here/i)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    });
  });

  describe("left rail", () => {
    it("resizes by its own handle and collapses to a slim strip, both persisted like the chat rails", async () => {
      mockProject();

      renderStation();
      await screen.findByRole("tree", { name: "Project files" });

      expect(screen.getByRole("separator", { name: "Resize Project" })).toBeInTheDocument();
      const user = userEvent.setup();

      await user.click(screen.getByRole("button", { name: "Collapse Project" }));
      expect(screen.queryByRole("tree", { name: "Project files" })).not.toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Open Project" }));
      expect(await screen.findByRole("tree", { name: "Project files" })).toBeInTheDocument();
    });
  });

  describe("note highlight", () => {
    it("highlights an open note's span in the reading view, and clicking it opens the same thread", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([note()]);
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      const paragraph = (await screen.findByText("Hello")).closest("p");
      if (!paragraph) throw new Error("expected the rendered content in a paragraph");

      await waitFor(() => expect(paragraph.className).toContain("note-highlight"));

      await user.click(paragraph);

      const card = await screen.findByRole("note");
      expect(card).toHaveTextContent("a note");
    });

    it("never highlights a resolved thread's span", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([note({ status: "resolved" })]);
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      const paragraph = (await screen.findByText("Hello")).closest("p");
      if (!paragraph) throw new Error("expected the rendered content in a paragraph");

      expect(paragraph.className).not.toContain("note-highlight");
    });

    it("the margin dot still opens its own thread card, as a working affordance on its own", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([note()]);
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      const paragraph = (await screen.findByText("Hello")).closest("p");
      if (!paragraph) throw new Error("expected the rendered content in a paragraph");

      // The span click opens its own popover first, and closes on an outside click.
      await user.click(paragraph);
      expect(await screen.findByRole("note")).toHaveTextContent("a note");
      await user.click(screen.getByTestId("reading-scroll"));
      expect(screen.queryByRole("note")).not.toBeInTheDocument();

      // The dot, a wholly separate affordance, still opens its own card too.
      await user.hover(screen.getByRole("button", { name: "Note on line 1" }));
      expect(screen.getByRole("note")).toHaveTextContent("a note");
    });

    it("clicking each of two notes anchored in the same block opens its own thread, not the other's", async () => {
      mockProject();
      vi.mocked(projectsApi.notes).mockResolvedValue([
        note({ id: "note-1", text: "first note", start: 0, end: 2 }),
        note({ id: "note-2", text: "second note", start: 2, end: 5 }),
      ]);
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      const paragraph = (await screen.findByText("Hello")).closest("p");
      if (!paragraph) throw new Error("expected the rendered content in a paragraph");
      const textNode = paragraph.firstChild;
      if (!textNode) throw new Error("expected the paragraph to have text content");

      clickAtOffset(paragraph, textNode, 1);
      expect(await screen.findByRole("note")).toHaveTextContent("first note");

      await user.click(screen.getByTestId("reading-scroll"));
      expect(screen.queryByRole("note")).not.toBeInTheDocument();

      clickAtOffset(paragraph, textNode, 3);
      expect(await screen.findByRole("note")).toHaveTextContent("second note");
    });

    it("clicking each of two notes anchored in different blocks opens its own thread", async () => {
      mockProject();
      vi.mocked(projectsApi.readFile).mockResolvedValue({
        content: "Hello\n\nWorld",
        hash: "h1",
        obsidian: false,
      });
      vi.mocked(projectsApi.notes).mockResolvedValue([
        note({ id: "note-1", text: "first note", start: 0, end: 5, line: 1 }),
        note({ id: "note-2", text: "second note", start: 7, end: 12, line: 3 }),
      ]);
      const user = userEvent.setup();

      renderStation();
      await user.click(await screen.findByRole("button", { name: "readme.md" }));
      const first = (await screen.findByText("Hello")).closest("p");
      const second = (await screen.findByText("World")).closest("p");
      if (!first || !second) throw new Error("expected two rendered paragraphs");

      await user.click(first);
      expect(await screen.findByRole("note")).toHaveTextContent("first note");

      await user.click(screen.getByTestId("reading-scroll"));
      expect(screen.queryByRole("note")).not.toBeInTheDocument();

      await user.click(second);
      expect(await screen.findByRole("note")).toHaveTextContent("second note");
    });
  });
});
