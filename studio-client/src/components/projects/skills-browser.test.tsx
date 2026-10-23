import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, type SkillCard, projectsApi } from "@/api/projects";
import { render } from "@/shared/test-support/render";

import { SkillsBrowser } from "./skills-browser";

vi.mock("@/api/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/projects")>();
  return {
    ...actual,
    projectsApi: {
      ...actual.projectsApi,
      skills: vi.fn(),
      installSkill: vi.fn(),
    },
  };
});

const SKILLS: SkillCard[] = [
  {
    name: "summarize",
    description: "Summarizes a note.",
    library: "team-lib",
    path: "libs/team-lib/summarize/SKILL.md",
    valid: true,
    error: null,
  },
  {
    name: "broken-one",
    description: "",
    library: "team-lib",
    path: "libs/team-lib/broken-one/SKILL.md",
    valid: false,
    error: "missing frontmatter",
  },
  {
    name: "core-search",
    description: "Ships with the app.",
    library: "builtin",
    path: "builtin/core-search/SKILL.md",
    valid: true,
    error: null,
  },
];

function renderBrowser(onOpen = vi.fn()) {
  const result = render(<SkillsBrowser projectId="proj-1" onOpen={onOpen} />);
  return { ...result, onOpen };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("<SkillsBrowser />", () => {
  it("renders every skill as a full-width row with its full name, a source badge, and a clamped description", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue(SKILLS);

    renderBrowser();

    const valid = await screen.findByTestId("skill-card-summarize");
    expect(within(valid).getByText("summarize")).toBeInTheDocument();
    expect(within(valid).getByText("team-lib")).toBeInTheDocument();
    const description = within(valid).getByText("Summarizes a note.");
    expect(description.className).toContain("line-clamp-2");
    expect(valid).toHaveAccessibleName("Open summarize");
    expect(valid).toHaveAttribute("data-skill-state", "valid");

    const broken = screen.getByTestId("skill-card-broken-one");
    expect(within(broken).getByText("missing frontmatter")).toBeInTheDocument();
    expect(broken).toHaveAccessibleName("Open broken-one");
    expect(broken).toHaveAttribute("data-skill-state", "broken");

    const builtin = screen.getByTestId("skill-card-core-search");
    expect(within(builtin).getByText("builtin")).toBeInTheDocument();
    expect(builtin).toHaveAccessibleName("Open core-search");
    expect(builtin).toHaveAttribute("data-skill-state", "builtin");
  });

  it("opens the reader pane with the row's own skill card, for a valid one and for a broken one", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue(SKILLS);
    const user = userEvent.setup();
    const { onOpen } = renderBrowser();

    const valid = await screen.findByTestId("skill-card-summarize");
    await user.click(valid);
    expect(onOpen).toHaveBeenCalledWith(SKILLS[0]);

    const broken = screen.getByTestId("skill-card-broken-one");
    await user.click(broken);
    expect(onOpen).toHaveBeenCalledWith(SKILLS[1]);
  });

  it("opens a builtin skill's own card too, since the host reads it read-only rather than through the file path", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue(SKILLS);
    const user = userEvent.setup();
    const { onOpen } = renderBrowser();

    const builtin = await screen.findByTestId("skill-card-core-search");
    await user.click(builtin);

    expect(onOpen).toHaveBeenCalledWith(SKILLS[2]);
  });

  it("New Skill offers only the non-builtin libraries present, creates, and opens the result", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue(SKILLS);
    vi.mocked(projectsApi.installSkill).mockResolvedValue("libs/team-lib/new-thing/SKILL.md");
    const user = userEvent.setup();
    const { onOpen } = renderBrowser();

    await screen.findByTestId("skill-card-summarize");
    await user.click(screen.getByRole("button", { name: "New Skill" }));

    const dialog = screen.getByRole("dialog");
    const librarySelect = within(dialog).getByLabelText("Library");
    expect(within(librarySelect).getAllByRole("option")).toHaveLength(1);
    expect(within(librarySelect).getByRole("option", { name: "team-lib" })).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText("Name"), "new-thing");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await vi.waitFor(() =>
      expect(projectsApi.installSkill).toHaveBeenCalledWith("proj-1", "team-lib", "new-thing"),
    );
    await vi.waitFor(() =>
      expect(onOpen).toHaveBeenCalledWith({
        name: "new-thing",
        description: "",
        library: "team-lib",
        path: "libs/team-lib/new-thing/SKILL.md",
        valid: true,
        error: null,
      }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("disables Create until a name is typed", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue(SKILLS);
    const user = userEvent.setup();
    renderBrowser();

    await screen.findByTestId("skill-card-summarize");
    await user.click(screen.getByRole("button", { name: "New Skill" }));

    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });

  it("surfaces a failed creation inline, without closing the dialog", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue(SKILLS);
    vi.mocked(projectsApi.installSkill).mockRejectedValue(new ApiError(400, "name already taken"));
    const user = userEvent.setup();
    const { onOpen } = renderBrowser();

    await screen.findByTestId("skill-card-summarize");
    await user.click(screen.getByRole("button", { name: "New Skill" }));
    await user.type(screen.getByLabelText("Name"), "dup");
    await user.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("name already taken");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("cancels the New Skill dialog without creating anything", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue(SKILLS);
    const user = userEvent.setup();
    renderBrowser();

    await screen.findByTestId("skill-card-summarize");
    await user.click(screen.getByRole("button", { name: "New Skill" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(projectsApi.installSkill).not.toHaveBeenCalled();
  });

  it("clears the failed name and error on cancel then reopen", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue(SKILLS);
    vi.mocked(projectsApi.installSkill).mockRejectedValue(new ApiError(400, "name already taken"));
    const user = userEvent.setup();
    renderBrowser();

    await screen.findByTestId("skill-card-summarize");
    await user.click(screen.getByRole("button", { name: "New Skill" }));
    await user.type(screen.getByLabelText("Name"), "dup");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("name already taken");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "New Skill" }));

    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("omits New Skill when no non-builtin library is present", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue([{ ...SKILLS[2] }]);

    renderBrowser();

    await screen.findByTestId("skill-card-core-search");
    expect(screen.queryByRole("button", { name: "New Skill" })).not.toBeInTheDocument();
  });

  it("names the missing library config in the empty state, promising no hidden button", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue([]);

    renderBrowser();

    const empty = await screen.findByText(/no skill libraries/i);
    expect(empty.parentElement).toHaveTextContent(".aifactory/config.json");
    expect(screen.queryByText(/new skill/i)).not.toBeInTheDocument();
  });

  it("lists the project it was given, not whichever project is active", async () => {
    vi.mocked(projectsApi.skills).mockResolvedValue(SKILLS);

    render(<SkillsBrowser projectId="proj-other" onOpen={vi.fn()} />);

    await vi.waitFor(() => expect(projectsApi.skills).toHaveBeenCalledWith("proj-other"));
  });

  it("shows the backend's refusal when the listing fails to load", async () => {
    vi.mocked(projectsApi.skills).mockRejectedValue(new ApiError(500, "database unavailable"));

    renderBrowser();

    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
  });
});
