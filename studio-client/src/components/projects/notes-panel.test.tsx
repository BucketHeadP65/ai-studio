import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type NoteListingEntry, projectsApi } from "@/api/projects";
import { render } from "@/shared/test-support/render";

import { NotesPanel } from "./notes-panel";

vi.mock("@/api/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/projects")>();
  return {
    projectKeys: actual.projectKeys,
    projectsApi: {
      notes: vi.fn(),
    },
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function note(overrides: Partial<NoteListingEntry> = {}): NoteListingEntry {
  return {
    id: "n0",
    anchor: { exact: "x", prefix: "", suffix: "" },
    text: "a note",
    created_at: "2026-08-23T00:00:00Z",
    archived: false,
    replies: [],
    status: "open",
    path: "docs/a.md",
    orphaned: false,
    line: 1,
    start: 0,
    end: 1,
    ...overrides,
  };
}

const NOTES: NoteListingEntry[] = [
  note({
    id: "n1",
    anchor: { exact: "alpha", prefix: "", suffix: "" },
    text: "first note",
    path: "docs/a.md",
    line: 3,
  }),
  note({
    id: "n2",
    anchor: { exact: "beta", prefix: "", suffix: "" },
    text: "second note, anchor moved",
    path: "docs/b.md",
    orphaned: true,
    line: 9,
  }),
  note({
    id: "n3",
    anchor: { exact: "gamma", prefix: "", suffix: "" },
    text: "archived note",
    archived: true,
    path: "docs/a.md",
    line: 1,
  }),
];

describe("<NotesPanel />", () => {
  it("groups notes by path, hides archived notes by default, and flags an orphaned note", async () => {
    vi.mocked(projectsApi.notes).mockResolvedValue(NOTES);

    render(<NotesPanel projectId="proj-1" onOpen={vi.fn()} />);

    expect(await screen.findByText("docs/a.md")).toBeInTheDocument();
    expect(screen.getByText("docs/b.md")).toBeInTheDocument();
    expect(screen.getByText("first note")).toBeInTheDocument();
    expect(screen.getByText("second note, anchor moved")).toBeInTheDocument();
    expect(screen.getByText(/anchor lost/i)).toBeInTheDocument();
    expect(screen.queryByText("archived note")).not.toBeInTheDocument();
  });

  it("the Show archived toggle reveals archived notes", async () => {
    vi.mocked(projectsApi.notes).mockResolvedValue(NOTES);
    const user = userEvent.setup();

    render(<NotesPanel projectId="proj-1" onOpen={vi.fn()} />);
    await screen.findByText("first note");
    expect(screen.queryByText("archived note")).not.toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /show archived/i }));

    expect(screen.getByText("archived note")).toBeInTheDocument();
  });

  it("a jump action calls onOpen with the note's path, line, and id", async () => {
    vi.mocked(projectsApi.notes).mockResolvedValue(NOTES);
    const onOpen = vi.fn();
    const user = userEvent.setup();

    render(<NotesPanel projectId="proj-1" onOpen={onOpen} />);
    const row = (await screen.findByText("first note")).closest("li");
    if (!row) throw new Error("expected the note to render inside a list item");

    await user.click(within(row).getByRole("button", { name: /jump/i }));

    expect(onOpen).toHaveBeenCalledWith("docs/a.md", 3, "n1");
  });

  it("clicking the note's own text also calls onOpen, so the thread opens without Jump", async () => {
    vi.mocked(projectsApi.notes).mockResolvedValue(NOTES);
    const onOpen = vi.fn();
    const user = userEvent.setup();

    render(<NotesPanel projectId="proj-1" onOpen={onOpen} />);

    await user.click(await screen.findByRole("button", { name: "first note" }));

    expect(onOpen).toHaveBeenCalledWith("docs/a.md", 3, "n1");
  });

  it("clicking an orphaned note's text still opens the file, with a null line", async () => {
    vi.mocked(projectsApi.notes).mockResolvedValue([
      note({ id: "n9", text: "gone missing", path: "docs/c.md", orphaned: true, line: null }),
    ]);
    const onOpen = vi.fn();
    const user = userEvent.setup();

    render(<NotesPanel projectId="proj-1" onOpen={onOpen} />);

    await user.click(await screen.findByRole("button", { name: "gone missing" }));

    expect(onOpen).toHaveBeenCalledWith("docs/c.md", null, "n9");
  });

  it("flags a resolved note with a Resolved badge", async () => {
    vi.mocked(projectsApi.notes).mockResolvedValue([
      note({ id: "n1", text: "settled note", status: "resolved" }),
    ]);

    render(<NotesPanel projectId="proj-1" onOpen={vi.fn()} />);

    const row = (await screen.findByText("settled note")).closest("li");
    if (!row) throw new Error("expected the note to render inside a list item");
    expect(within(row).getByText("Resolved")).toBeInTheDocument();
  });

  it("lists open notes before resolved ones within the same file", async () => {
    vi.mocked(projectsApi.notes).mockResolvedValue([
      note({ id: "n1", text: "settled first", status: "resolved", line: 1 }),
      note({ id: "n2", text: "still open", status: "open", line: 2 }),
    ]);

    render(<NotesPanel projectId="proj-1" onOpen={vi.fn()} />);

    const rows = await screen.findAllByRole("listitem");
    const texts = rows.map((row) => row.textContent);
    expect(texts.findIndex((text) => text?.includes("still open"))).toBeLessThan(
      texts.findIndex((text) => text?.includes("settled first")),
    );
  });

  it("shows an empty state when there are no notes", async () => {
    vi.mocked(projectsApi.notes).mockResolvedValue([]);

    render(<NotesPanel projectId="proj-1" onOpen={vi.fn()} />);

    expect(await screen.findByText(/no notes yet/i)).toBeInTheDocument();
  });

  it("shows the backend's refusal when the listing fails to load", async () => {
    vi.mocked(projectsApi.notes).mockRejectedValue(new Error("database unavailable"));

    render(<NotesPanel projectId="proj-1" onOpen={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
  });
});
