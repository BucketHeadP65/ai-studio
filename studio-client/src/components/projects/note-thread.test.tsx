import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type NoteListingEntry, projectsApi } from "@/api/projects";
import { render } from "@/test-support/render";

import { NoteThread } from "./note-thread";

vi.mock("@/api/projects", () => ({
  projectsApi: {
    replyToNote: vi.fn(),
    resolveNote: vi.fn(),
    reopenNote: vi.fn(),
    deleteNote: vi.fn(),
    updateNote: vi.fn(),
    updateReply: vi.fn(),
  },
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function note(overrides: Partial<NoteListingEntry> = {}): NoteListingEntry {
  return {
    id: "note-1",
    anchor: { exact: "the quoted span", prefix: "before ", suffix: " after" },
    text: "a note about this",
    created_at: "2026-08-23T00:00:00Z",
    archived: false,
    replies: [],
    status: "open",
    path: "docs/guide.md",
    orphaned: false,
    line: 4,
    start: 40,
    end: 56,
    ...overrides,
  };
}

describe("<NoteThread />", () => {
  it("renders the anchored quote and the original comment", () => {
    render(
      <NoteThread projectId="proj-1" path="docs/guide.md" note={note()} onChanged={vi.fn()} />,
    );

    expect(screen.getByText("the quoted span")).toBeInTheDocument();
    expect(screen.getByText("a note about this")).toBeInTheDocument();
  });

  it("shows an anchor-lost badge when orphaned", () => {
    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({ orphaned: true })}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByText(/anchor lost/i)).toBeInTheDocument();
  });

  it("shows a Resolved badge and offers Reopen when the thread is resolved", () => {
    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({ status: "resolved" })}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByText("Resolved")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reopen" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resolve" })).not.toBeInTheDocument();
  });

  it("drops the reply composer and the Reply action once a thread is resolved", () => {
    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({ status: "resolved" })}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.queryByPlaceholderText("Reply")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reply" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("explains an orphaned note's lost anchor, in words alongside the pill", () => {
    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({ orphaned: true })}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByText("The lines this note pointed at were rewritten.")).toBeInTheDocument();
  });

  it("renders an owner reply toned like chat's You, and an agent reply with its own byline", () => {
    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({
          replies: [
            { author: "owner", text: "any update?", at: "2026-08-23T01:00:00Z" },
            { author: "agent", text: "fixed it", at: "2026-08-23T02:00:00Z" },
          ],
        })}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.getByText("any update?")).toBeInTheDocument();
    expect(screen.getByText("Orchestrator agent, not a person")).toBeInTheDocument();
    expect(screen.getByText("fixed it")).toBeInTheDocument();
  });

  it("renders the quote, note text, and replies at body size, never micro-typography", () => {
    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({
          replies: [{ author: "owner", text: "any update?", at: "2026-08-23T01:00:00Z" }],
        })}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByText("the quoted span").className).toContain("text-body");
    expect(screen.getByText("a note about this").className).toContain("text-body");
    expect(screen.getByText("any update?").closest("p")?.className).toContain("text-body");
  });

  it("the anchored quote clamps to 3 lines and carries the full text as its title", () => {
    render(
      <NoteThread projectId="proj-1" path="docs/guide.md" note={note()} onChanged={vi.fn()} />,
    );

    const quote = screen.getByText("the quoted span");
    expect(quote.className).toContain("line-clamp-3");
    expect(quote).toHaveAttribute("title", "the quoted span");
  });

  it("an owner reply offers an Edit affordance, an agent reply does not", () => {
    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({
          replies: [
            { author: "owner", text: "any update?", at: "2026-08-23T01:00:00Z" },
            { author: "agent", text: "fixed it", at: "2026-08-23T02:00:00Z" },
          ],
        })}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getAllByRole("button", { name: "Edit reply" })).toHaveLength(1);
  });

  it("editing the note text switches to a textarea, Save posts the new text and reports the change", async () => {
    vi.mocked(projectsApi.updateNote).mockResolvedValue({ ...note(), text: "revised text" });
    const onChanged = vi.fn();
    const user = userEvent.setup();

    render(
      <NoteThread projectId="proj-1" path="docs/guide.md" note={note()} onChanged={onChanged} />,
    );

    await user.click(screen.getByRole("button", { name: "Edit note" }));
    const textarea = screen.getByDisplayValue("a note about this");
    await user.clear(textarea);
    await user.type(textarea, "revised text");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(projectsApi.updateNote).toHaveBeenCalledWith("proj-1", "note-1", {
        path: "docs/guide.md",
        text: "revised text",
      }),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("Cancel on a note-text edit returns to the read view without saving", async () => {
    const user = userEvent.setup();

    render(
      <NoteThread projectId="proj-1" path="docs/guide.md" note={note()} onChanged={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "Edit note" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.getByText("a note about this")).toBeInTheDocument();
    expect(projectsApi.updateNote).not.toHaveBeenCalled();
  });

  it("editing an owner reply posts its index and text, and reports the change", async () => {
    vi.mocked(projectsApi.updateReply).mockResolvedValue({
      ...note(),
      replies: [{ author: "owner", text: "actually done", at: "2026-08-23T01:00:00Z" }],
    });
    const onChanged = vi.fn();
    const user = userEvent.setup();

    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({
          replies: [{ author: "owner", text: "any update?", at: "2026-08-23T01:00:00Z" }],
        })}
        onChanged={onChanged}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Edit reply" }));
    const textarea = screen.getByDisplayValue("any update?");
    await user.clear(textarea);
    await user.type(textarea, "actually done");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(projectsApi.updateReply).toHaveBeenCalledWith("proj-1", "note-1", {
        path: "docs/guide.md",
        replyIndex: 0,
        text: "actually done",
      }),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("a failed reply edit renders inline, without leaving the edit view", async () => {
    vi.mocked(projectsApi.updateReply).mockRejectedValue(
      new Error("agent replies cannot be edited"),
    );
    const user = userEvent.setup();

    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({
          replies: [{ author: "owner", text: "any update?", at: "2026-08-23T01:00:00Z" }],
        })}
        onChanged={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Edit reply" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("agent replies cannot be edited");
    expect(screen.getByDisplayValue("any update?")).toBeInTheDocument();
  });

  it("Reply is disabled until text is typed, then posts it and reports the change", async () => {
    vi.mocked(projectsApi.replyToNote).mockResolvedValue({
      ...note(),
      replies: [{ author: "owner", text: "on it", at: "2026-08-23T01:00:00Z" }],
    });
    const onChanged = vi.fn();
    const user = userEvent.setup();

    render(
      <NoteThread projectId="proj-1" path="docs/guide.md" note={note()} onChanged={onChanged} />,
    );

    const replyButton = screen.getByRole("button", { name: "Reply" });
    expect(replyButton).toBeDisabled();

    await user.type(screen.getByPlaceholderText("Reply"), "on it");
    expect(replyButton).not.toBeDisabled();
    await user.click(replyButton);

    await vi.waitFor(() =>
      expect(projectsApi.replyToNote).toHaveBeenCalledWith(
        "proj-1",
        "note-1",
        "docs/guide.md",
        "on it",
      ),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("a failed reply renders inline, without clearing the composer", async () => {
    vi.mocked(projectsApi.replyToNote).mockRejectedValue(new Error("network down"));
    const user = userEvent.setup();

    render(
      <NoteThread projectId="proj-1" path="docs/guide.md" note={note()} onChanged={vi.fn()} />,
    );

    await user.type(screen.getByPlaceholderText("Reply"), "on it");
    await user.click(screen.getByRole("button", { name: "Reply" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("network down");
    expect(screen.getByPlaceholderText("Reply")).toHaveValue("on it");
  });

  it("Resolve calls the resolve endpoint and reports the change", async () => {
    vi.mocked(projectsApi.resolveNote).mockResolvedValue({ ...note(), status: "resolved" });
    const onChanged = vi.fn();
    const user = userEvent.setup();

    render(
      <NoteThread projectId="proj-1" path="docs/guide.md" note={note()} onChanged={onChanged} />,
    );

    await user.click(screen.getByRole("button", { name: "Resolve" }));

    await vi.waitFor(() =>
      expect(projectsApi.resolveNote).toHaveBeenCalledWith("proj-1", "note-1", "docs/guide.md"),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("Reopen calls the reopen endpoint and reports the change", async () => {
    vi.mocked(projectsApi.reopenNote).mockResolvedValue({ ...note(), status: "open" });
    const onChanged = vi.fn();
    const user = userEvent.setup();

    render(
      <NoteThread
        projectId="proj-1"
        path="docs/guide.md"
        note={note({ status: "resolved" })}
        onChanged={onChanged}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Reopen" }));

    await vi.waitFor(() =>
      expect(projectsApi.reopenNote).toHaveBeenCalledWith("proj-1", "note-1", "docs/guide.md"),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("Delete asks for confirmation before calling the delete endpoint", async () => {
    vi.mocked(projectsApi.deleteNote).mockResolvedValue(undefined);
    const onChanged = vi.fn();
    const user = userEvent.setup();

    render(
      <NoteThread projectId="proj-1" path="docs/guide.md" note={note()} onChanged={onChanged} />,
    );

    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(projectsApi.deleteNote).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Delete this note?")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await vi.waitFor(() =>
      expect(projectsApi.deleteNote).toHaveBeenCalledWith("proj-1", "note-1", "docs/guide.md"),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("canceling the delete confirmation does not call the delete endpoint", async () => {
    const user = userEvent.setup();

    render(
      <NoteThread projectId="proj-1" path="docs/guide.md" note={note()} onChanged={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByText("Delete this note?")).not.toBeInTheDocument();
    expect(projectsApi.deleteNote).not.toHaveBeenCalled();
  });
});
