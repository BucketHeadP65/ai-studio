import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FileEntry, FileNoteSummary } from "@/api/projects";
import { render } from "@/shared/test-support/render";

vi.mock("@/api/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/projects")>();
  return {
    projectKeys: actual.projectKeys,
    projectsApi: {
      files: vi.fn(),
      readFile: vi.fn(),
      createFile: vi.fn(),
      createDir: vi.fn(),
      writeFile: vi.fn(),
      renameFile: vi.fn(),
      deleteFile: vi.fn(),
      notesSummary: vi.fn(),
    },
  };
});

// Imported after the mock so the component sees the mocked module.
import { projectsApi } from "@/api/projects";

import { FileTree } from "./file-tree";

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(projectsApi.notesSummary).mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ENTRIES: FileEntry[] = [
  { path: "readme.md", kind: "file", size: 10 },
  { path: "notes", kind: "dir", size: 0 },
  { path: "notes/todo.md", kind: "file", size: 20 },
  { path: "notes/image.png", kind: "file", size: 500 },
  // "assets" has no markdown anywhere beneath it, so it -- and its file --
  // must not appear at all.
  { path: "assets", kind: "dir", size: 0 },
  { path: "assets/logo.png", kind: "file", size: 900 },
  // "docs/sub" has markdown two levels down and must survive; "docs/other"
  // has none and must not, even though "docs" itself does.
  { path: "docs", kind: "dir", size: 0 },
  { path: "docs/sub", kind: "dir", size: 0 },
  { path: "docs/sub/deep.md", kind: "file", size: 30 },
  { path: "docs/other", kind: "dir", size: 0 },
  { path: "docs/other/image.png", kind: "file", size: 40 },
];

function mockFiles(entries: FileEntry[]): void {
  vi.mocked(projectsApi.files).mockResolvedValue(entries);
}

async function openRowMenu(
  user: ReturnType<typeof userEvent.setup>,
  path: string,
): Promise<HTMLElement> {
  await user.click(await screen.findByRole("button", { name: `Actions for ${path}` }));
  return screen.getByRole("menu", { name: `Actions for ${path}` });
}

describe("<FileTree />", () => {
  it("renders the flat listing as a nested tree, directories before files", async () => {
    mockFiles(ENTRIES);

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);

    const tree = await screen.findByRole("tree", { name: "Project files" });
    const items = within(tree).getAllByRole("treeitem", { hidden: true });
    // Root-level directories ("docs", "notes") sort before the root-level file ("readme.md").
    expect(items[0]).toHaveTextContent("docs");
    expect(within(tree).getByText("readme.md")).toBeInTheDocument();
    // children of "notes" are expanded by default.
    expect(within(tree).getByText("todo.md")).toBeInTheDocument();
    // "docs/sub/deep.md" pulls both "docs" and "docs/sub" into the tree.
    expect(within(tree).getByText("docs")).toBeInTheDocument();
    expect(within(tree).getByText("sub")).toBeInTheDocument();
    expect(within(tree).getByText("deep.md")).toBeInTheDocument();
  });

  it("shows the full name and title on every row, at the compact caption scale", async () => {
    mockFiles([{ path: "orchestrating-a-very-long-workflow-name.md", kind: "file", size: 10 }]);

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);

    const label = await screen.findByText("orchestrating-a-very-long-workflow-name.md");
    expect(label).toHaveAttribute("title", "orchestrating-a-very-long-workflow-name.md");
    expect(label.className).toContain("text-caption");
    expect(label.className).toContain("truncate");
  });

  it("lists only markdown files, hiding every directory with no markdown beneath it", async () => {
    mockFiles(ENTRIES);
    const onSelectFile = vi.fn();
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={onSelectFile} />);
    await screen.findByRole("tree", { name: "Project files" });

    // Non-markdown files never appear, not even as inert text.
    expect(screen.queryByText("image.png")).not.toBeInTheDocument();
    expect(screen.queryByText("logo.png")).not.toBeInTheDocument();
    // A directory with no markdown anywhere beneath it is gone entirely.
    expect(screen.queryByText("assets")).not.toBeInTheDocument();
    expect(screen.queryByText("other")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "todo.md" }));
    expect(onSelectFile).toHaveBeenCalledWith("notes/todo.md");
  });

  it("collapses and re-expands a directory", async () => {
    mockFiles(ENTRIES);
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
    await screen.findByRole("tree", { name: "Project files" });
    expect(screen.getByText("todo.md")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Collapse notes" }));
    expect(screen.queryByText("todo.md")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Expand notes" }));
    expect(screen.getByText("todo.md")).toBeInTheDocument();
  });

  it("creates a file at the root through createFile, never overwriting via a write", async () => {
    mockFiles([]);
    vi.mocked(projectsApi.createFile).mockResolvedValue("hash-1");
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
    await screen.findByText("No files yet.");

    await user.click(screen.getByRole("button", { name: "New file at root" }));
    const form = screen.getByRole("form", { name: "New file at root" });
    await user.type(within(form).getByRole("textbox"), "index.md");
    mockFiles([{ path: "index.md", kind: "file", size: 0 }]);
    await user.click(within(form).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(projectsApi.createFile).toHaveBeenCalledWith("proj-1", "index.md"));
    expect(projectsApi.writeFile).not.toHaveBeenCalled();
    await waitFor(() => expect(projectsApi.files).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("button", { name: "index.md" })).toBeInTheDocument();
  });

  it("creates a file inside a directory via its row menu, with a path joined to that directory", async () => {
    mockFiles(ENTRIES);
    vi.mocked(projectsApi.createFile).mockResolvedValue("hash-2");
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
    await screen.findByRole("tree", { name: "Project files" });

    const menu = await openRowMenu(user, "notes");
    await user.click(within(menu).getByRole("menuitem", { name: "New file" }));
    const form = screen.getByRole("form", { name: "New file in notes" });
    await user.type(within(form).getByRole("textbox"), "second.md");
    await user.click(within(form).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(projectsApi.createFile).toHaveBeenCalledWith("proj-1", "notes/second.md"),
    );
  });

  it("creates a folder inside a directory via its row menu, through createDir", async () => {
    mockFiles(ENTRIES);
    vi.mocked(projectsApi.createDir).mockResolvedValue(undefined);
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
    await screen.findByRole("tree", { name: "Project files" });

    const menu = await openRowMenu(user, "notes");
    await user.click(within(menu).getByRole("menuitem", { name: "New folder" }));
    const form = screen.getByRole("form", { name: "New folder in notes" });
    await user.type(within(form).getByRole("textbox"), "archive");
    await user.click(within(form).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(projectsApi.createDir).toHaveBeenCalledWith("proj-1", "notes/archive"),
    );
    expect(projectsApi.createFile).not.toHaveBeenCalled();
  });

  it("surfaces a refused creation inline, keeping the form open", async () => {
    mockFiles(ENTRIES);
    vi.mocked(projectsApi.createFile).mockRejectedValue(new Error("'notes/todo.md' exists."));
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
    await screen.findByRole("tree", { name: "Project files" });

    const menu = await openRowMenu(user, "notes");
    await user.click(within(menu).getByRole("menuitem", { name: "New file" }));
    const form = screen.getByRole("form", { name: "New file in notes" });
    await user.type(within(form).getByRole("textbox"), "todo.md");
    await user.click(within(form).getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("'notes/todo.md' exists.");
    expect(screen.getByRole("form", { name: "New file in notes" })).toBeInTheDocument();
  });

  it("renames a file to a sibling path via its row menu, and re-fetches the listing", async () => {
    mockFiles(ENTRIES);
    vi.mocked(projectsApi.renameFile).mockResolvedValue(undefined);
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
    await screen.findByRole("tree", { name: "Project files" });

    const menu = await openRowMenu(user, "notes/todo.md");
    await user.click(within(menu).getByRole("menuitem", { name: "Rename" }));
    const form = screen.getByRole("form", { name: "Rename notes/todo.md" });
    const input = within(form).getByRole("textbox");
    await user.clear(input);
    await user.type(input, "renamed.md");
    mockFiles([
      { path: "readme.md", kind: "file", size: 10 },
      { path: "notes", kind: "dir", size: 0 },
      { path: "notes/renamed.md", kind: "file", size: 20 },
    ]);
    await user.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(projectsApi.renameFile).toHaveBeenCalledWith(
        "proj-1",
        "notes/todo.md",
        "notes/renamed.md",
      ),
    );
    await waitFor(() => expect(projectsApi.files).toHaveBeenCalledTimes(2));
  });

  it("deletes only after the confirm dialog is confirmed, then re-fetches", async () => {
    mockFiles(ENTRIES);
    vi.mocked(projectsApi.deleteFile).mockResolvedValue(undefined);
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
    await screen.findByRole("tree", { name: "Project files" });

    let menu = await openRowMenu(user, "readme.md");
    await user.click(within(menu).getByRole("menuitem", { name: "Delete" }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(projectsApi.deleteFile).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    menu = await openRowMenu(user, "readme.md");
    await user.click(within(menu).getByRole("menuitem", { name: "Delete" }));
    mockFiles([
      { path: "notes", kind: "dir", size: 0 },
      { path: "notes/todo.md", kind: "file", size: 20 },
      { path: "notes/image.png", kind: "file", size: 500 },
    ]);
    await user.click(screen.getByRole("button", { name: "Delete it" }));

    await waitFor(() => expect(projectsApi.deleteFile).toHaveBeenCalledWith("proj-1", "readme.md"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("calls onPathRemoved with the deleted path once the delete succeeds", async () => {
    mockFiles(ENTRIES);
    vi.mocked(projectsApi.deleteFile).mockResolvedValue(undefined);
    const onPathRemoved = vi.fn();
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} onPathRemoved={onPathRemoved} />);
    await screen.findByRole("tree", { name: "Project files" });

    const menu = await openRowMenu(user, "readme.md");
    await user.click(within(menu).getByRole("menuitem", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Delete it" }));

    await waitFor(() => expect(onPathRemoved).toHaveBeenCalledWith("readme.md"));
  });

  it("calls onPathRenamed with the old and new path once the rename succeeds", async () => {
    mockFiles(ENTRIES);
    vi.mocked(projectsApi.renameFile).mockResolvedValue(undefined);
    const onPathRenamed = vi.fn();
    const user = userEvent.setup();

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} onPathRenamed={onPathRenamed} />);
    await screen.findByRole("tree", { name: "Project files" });

    const menu = await openRowMenu(user, "notes/todo.md");
    await user.click(within(menu).getByRole("menuitem", { name: "Rename" }));
    const form = screen.getByRole("form", { name: "Rename notes/todo.md" });
    const input = within(form).getByRole("textbox");
    await user.clear(input);
    await user.type(input, "renamed.md");
    await user.click(within(form).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(onPathRenamed).toHaveBeenCalledWith("notes/todo.md", "notes/renamed.md"),
    );
  });

  it("surfaces a fetch error and a mutation error inline", async () => {
    vi.mocked(projectsApi.files).mockRejectedValue(new Error("boom"));

    render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });

  describe("row action menu", () => {
    it("stays hidden until hover, opens on click, and closes on an outside click", async () => {
      mockFiles(ENTRIES);
      const user = userEvent.setup();

      render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
      await screen.findByRole("tree", { name: "Project files" });

      const trigger = screen.getByRole("button", { name: "Actions for readme.md" });
      expect(trigger.parentElement?.className).toContain("invisible");

      await user.click(trigger);
      expect(screen.getByRole("menu", { name: "Actions for readme.md" })).toBeInTheDocument();
      expect(trigger.parentElement?.className).toContain("visible");

      await user.click(document.body);
      expect(screen.queryByRole("menu", { name: "Actions for readme.md" })).not.toBeInTheDocument();
    });

    it("offers New file and New folder only on a directory row, never on a file row", async () => {
      mockFiles(ENTRIES);
      const user = userEvent.setup();

      render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
      await screen.findByRole("tree", { name: "Project files" });

      const dirMenu = await openRowMenu(user, "notes");
      expect(within(dirMenu).getByRole("menuitem", { name: "New file" })).toBeInTheDocument();
      expect(within(dirMenu).getByRole("menuitem", { name: "New folder" })).toBeInTheDocument();
      await user.click(document.body);

      const fileMenu = await openRowMenu(user, "notes/todo.md");
      expect(
        within(fileMenu).queryByRole("menuitem", { name: "New file" }),
      ).not.toBeInTheDocument();
      expect(
        within(fileMenu).queryByRole("menuitem", { name: "New folder" }),
      ).not.toBeInTheDocument();
    });
  });

  describe("note-status dot", () => {
    function summary(overrides: Partial<FileNoteSummary> = {}): FileNoteSummary {
      return {
        path: "readme.md",
        open_notes: 1,
        resolved_notes: 0,
        latest_agent_reply_at: null,
        ...overrides,
      };
    }

    it("renders no dot for a markdown file with no notes", async () => {
      mockFiles(ENTRIES);
      vi.mocked(projectsApi.notesSummary).mockResolvedValue([]);

      render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);
      await screen.findByRole("tree", { name: "Project files" });

      expect(screen.queryByLabelText(/agent activity/i)).not.toBeInTheDocument();
    });

    it("renders the seen tone when the file has notes but no unseen agent reply", async () => {
      mockFiles(ENTRIES);
      vi.mocked(projectsApi.notesSummary).mockResolvedValue([
        summary({ latest_agent_reply_at: null }),
      ]);

      render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);

      expect(await screen.findByLabelText(/has been seen/i)).toBeInTheDocument();
    });

    it("renders the new tone when the agent replied after the viewer's last recorded look", async () => {
      mockFiles(ENTRIES);
      vi.mocked(projectsApi.notesSummary).mockResolvedValue([
        summary({ latest_agent_reply_at: "2026-08-11T09:06:00+00:00" }),
      ]);

      render(<FileTree projectId="proj-1" onSelectFile={vi.fn()} />);

      expect(await screen.findByLabelText(/new agent activity/i)).toBeInTheDocument();
    });
  });
});
