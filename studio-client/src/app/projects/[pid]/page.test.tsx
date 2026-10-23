import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FileEntry, ProjectListingEntry } from "@/api/projects";
import { render } from "@/shared/test-support/render";

vi.mock("@/api/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/projects")>();
  return {
    projectKeys: actual.projectKeys,
    projectsApi: {
      list: vi.fn(),
      files: vi.fn(),
      readFile: vi.fn(),
      createFile: vi.fn(),
      writeFile: vi.fn(),
      renameFile: vi.fn(),
      deleteFile: vi.fn(),
      notes: vi.fn(),
    },
  };
});

vi.mock("next/navigation", () => ({
  useParams: () => ({ pid: "proj-1" }),
}));

// Imported after the mocks so the page sees the mocked modules.
import { projectsApi } from "@/api/projects";

import ProjectPage from "./page";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ENTRIES: FileEntry[] = [{ path: "readme.md", kind: "file", size: 10 }];

const PROJECTS: ProjectListingEntry[] = [
  { id: "proj-1", name: "Proj One", root_path: "/vault", missing: false, running_sessions: 0 },
];

function mockProject(): void {
  vi.mocked(projectsApi.list).mockResolvedValue(PROJECTS);
  vi.mocked(projectsApi.files).mockResolvedValue(ENTRIES);
  vi.mocked(projectsApi.notes).mockResolvedValue([]);
}

describe("ProjectPage", () => {
  it("mounts the station for the pid from the route params", async () => {
    mockProject();

    render(<ProjectPage />);

    expect(await screen.findByRole("tree", { name: "Project files" })).toBeInTheDocument();
    expect(projectsApi.files).toHaveBeenCalledWith("proj-1");
  });

  it("shows the detail header with the project's name and root path", async () => {
    mockProject();

    render(<ProjectPage />);

    expect(await screen.findByRole("heading", { level: 1, name: "Proj One" })).toBeInTheDocument();
    expect(screen.getByText("/vault")).toBeInTheDocument();
  });

  it("falls back to the raw id in the header while the project list is still loading", () => {
    vi.mocked(projectsApi.list).mockReturnValue(new Promise(() => {}));
    vi.mocked(projectsApi.files).mockReturnValue(new Promise(() => {}));
    vi.mocked(projectsApi.notes).mockReturnValue(new Promise(() => {}));

    render(<ProjectPage />);

    expect(screen.getByRole("heading", { level: 1, name: "proj-1" })).toBeInTheDocument();
  });
});
