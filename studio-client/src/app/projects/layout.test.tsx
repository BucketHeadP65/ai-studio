import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FileEntry, NoteListingEntry, Project, ProjectListingEntry } from "@/api/projects";
import { render } from "@/shared/test-support/render";

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
  useParams: vi.fn(() => ({})),
}));

vi.mock("@/api/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/projects")>();
  return {
    projectKeys: actual.projectKeys,
    projectsApi: {
      list: vi.fn(),
      files: vi.fn(),
      notes: vi.fn(),
      register: vi.fn(),
      unregister: vi.fn(),
    },
  };
});

// Imported after the mocks so the layout sees the mocked modules.
import { projectsApi } from "@/api/projects";
import { useParams } from "next/navigation";

import ProjectsLayout from "./layout";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

function project(overrides: Partial<ProjectListingEntry> = {}): ProjectListingEntry {
  return {
    id: "proj-1",
    name: "engram",
    root_path: "/home/user/engram",
    missing: false,
    running_sessions: 0,
    ...overrides,
  };
}

function entries(count: number): FileEntry[] {
  return Array.from({ length: count }, (_, index) => ({
    path: `file-${index}.md`,
    kind: "file" as const,
    size: 1,
  }));
}

function note(id: string, status: "open" | "resolved", archived: boolean): NoteListingEntry {
  return {
    id,
    anchor: { exact: "", prefix: "", suffix: "" },
    text: "note",
    created_at: "2026-08-23T00:00:00Z",
    archived,
    replies: [],
    status,
    path: "a.md",
    orphaned: false,
    line: 1,
    start: 0,
    end: 1,
  };
}

function openNotes(count: number): NoteListingEntry[] {
  return Array.from({ length: count }, (_, index) => note(`open-${index}`, "open", false));
}

function renderLayout() {
  return render(
    <ProjectsLayout>
      <div data-testid="child">right pane</div>
    </ProjectsLayout>,
  );
}

describe("ProjectsLayout", () => {
  it("shows the header summary with the project carrying the most open notes", async () => {
    vi.mocked(useParams).mockReturnValue({});
    vi.mocked(projectsApi.list).mockResolvedValue([
      project({ id: "proj-1", name: "engram" }),
      project({ id: "proj-2", name: "harness" }),
    ]);
    vi.mocked(projectsApi.files).mockResolvedValue(entries(2));
    vi.mocked(projectsApi.notes).mockImplementation((id: string) =>
      Promise.resolve(id === "proj-2" ? openNotes(3) : openNotes(1)),
    );

    renderLayout();

    expect(await screen.findByText("2 projects, 3 notes open on harness.")).toBeInTheDocument();
  });

  it("drops the notes clause when nothing is open anywhere", async () => {
    vi.mocked(useParams).mockReturnValue({});
    vi.mocked(projectsApi.list).mockResolvedValue([project()]);
    vi.mocked(projectsApi.files).mockResolvedValue(entries(1));
    vi.mocked(projectsApi.notes).mockResolvedValue([]);

    renderLayout();

    expect(await screen.findByText("1 project.")).toBeInTheDocument();
  });

  it("says no projects yet, in the header and in the rail, when the list is empty", async () => {
    vi.mocked(useParams).mockReturnValue({});
    vi.mocked(projectsApi.list).mockResolvedValue([]);

    renderLayout();

    expect(await screen.findAllByText("No projects yet.")).toHaveLength(2);
  });

  it("shows a rail row's file and open note counts", async () => {
    vi.mocked(useParams).mockReturnValue({});
    vi.mocked(projectsApi.list).mockResolvedValue([project({ id: "proj-1", name: "engram" })]);
    vi.mocked(projectsApi.files).mockResolvedValue(entries(4));
    vi.mocked(projectsApi.notes).mockResolvedValue(openNotes(2));

    renderLayout();

    expect(await screen.findByText("4 files, 2 notes open")).toBeInTheDocument();
  });

  it("marks the current route's project active, with a trailing open notes badge", async () => {
    vi.mocked(useParams).mockReturnValue({ pid: "proj-1" });
    vi.mocked(projectsApi.list).mockResolvedValue([project({ id: "proj-1", name: "engram" })]);
    vi.mocked(projectsApi.files).mockResolvedValue(entries(1));
    vi.mocked(projectsApi.notes).mockResolvedValue(openNotes(5));

    renderLayout();

    const row = await screen.findByRole("link", { name: /engram/ });
    expect(row).toHaveAttribute("aria-current", "page");
    expect(within(row).getByText("5")).toBeInTheDocument();
  });

  it("renders a missing project dimmed and unlinked", async () => {
    vi.mocked(useParams).mockReturnValue({});
    vi.mocked(projectsApi.list).mockResolvedValue([
      project({ id: "proj-2", name: "gone", missing: true }),
    ]);

    renderLayout();

    expect(await screen.findByText("gone")).toBeInTheDocument();
    expect(screen.getByText("Missing")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /gone/ })).not.toBeInTheDocument();
  });

  it("sets the routed project active in storage", async () => {
    vi.mocked(useParams).mockReturnValue({ pid: "proj-1" });
    vi.mocked(projectsApi.list).mockResolvedValue([project()]);
    vi.mocked(projectsApi.files).mockResolvedValue([]);
    vi.mocked(projectsApi.notes).mockResolvedValue([]);

    renderLayout();

    await waitFor(() =>
      expect(window.localStorage.getItem("engram.active-project-id")).toBe("proj-1"),
    );
  });

  it("shows the backend's refusal when the listing fails to load", async () => {
    vi.mocked(useParams).mockReturnValue({});
    vi.mocked(projectsApi.list).mockRejectedValue(new Error("database unavailable"));

    renderLayout();

    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
  });

  it("opening a folder through the dialog selects the new project and navigates to it", async () => {
    vi.mocked(useParams).mockReturnValue({});
    vi.mocked(projectsApi.list).mockResolvedValue([project()]);
    vi.mocked(projectsApi.files).mockResolvedValue(entries(1));
    vi.mocked(projectsApi.notes).mockResolvedValue([]);
    const registered: Project = {
      id: "proj-4",
      name: "new-one",
      root_path: "/home/user/new-one",
    };
    vi.mocked(projectsApi.register).mockResolvedValue(registered);
    const user = userEvent.setup();

    renderLayout();

    await user.click(await screen.findByRole("button", { name: "New project" }));
    await user.type(screen.getByLabelText("Absolute path"), "/home/user/new-one");
    await user.click(screen.getByRole("button", { name: "Open" }));

    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/projects/proj-4"));
    await waitFor(() =>
      expect(window.localStorage.getItem("engram.active-project-id")).toBe("proj-4"),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("ProjectsLayout, removing a project", () => {
  it("unregisters a project after the confirm, and the rail refetches", async () => {
    vi.mocked(useParams).mockReturnValue({});
    vi.mocked(projectsApi.list)
      .mockResolvedValueOnce([project({ id: "proj-1", name: "scratch", missing: true })])
      .mockResolvedValue([]);
    vi.mocked(projectsApi.files).mockResolvedValue(entries(0));
    vi.mocked(projectsApi.notes).mockResolvedValue(openNotes(0));
    vi.mocked(projectsApi.unregister).mockResolvedValue(undefined);

    renderLayout();
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Remove scratch from the workspace" }),
    );
    expect(
      screen.getByRole("dialog", { name: 'Remove "scratch" from the workspace?' }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(projectsApi.unregister).toHaveBeenCalledWith("proj-1"));
    expect(await screen.findAllByText("No projects yet.")).toHaveLength(2);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps the project and shows the refusal when the server says no", async () => {
    vi.mocked(useParams).mockReturnValue({});
    vi.mocked(projectsApi.list).mockResolvedValue([project({ id: "proj-1", name: "engram" })]);
    vi.mocked(projectsApi.files).mockResolvedValue(entries(1));
    vi.mocked(projectsApi.notes).mockResolvedValue(openNotes(0));
    vi.mocked(projectsApi.unregister).mockRejectedValue(
      new Error("a session in this project is answering"),
    );

    renderLayout();
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Remove engram from the workspace" }),
    );
    await user.click(screen.getByRole("button", { name: "Remove" }));

    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "a session in this project is answering",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    // The row itself is still in the DOM; the open confirm dialog aria-hides it, so
    // it is found by text rather than by its (currently hidden-from-a11y-tree) role.
    expect(screen.getByText("engram")).toBeInTheDocument();
  });
});
