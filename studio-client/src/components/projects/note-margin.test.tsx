import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type NoteListingEntry, projectsApi } from "@/api/projects";
import { render } from "@/test-support/render";

import { NoteMargin } from "./note-margin";

vi.mock("@/api/projects", () => ({
  projectsApi: {
    replyToNote: vi.fn(),
    resolveNote: vi.fn(),
    reopenNote: vi.fn(),
    deleteNote: vi.fn(),
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

describe("<NoteMargin />", () => {
  it("renders one dot per unarchived note, skipping archived ones", () => {
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[note({ id: "n1", line: 2 }), note({ id: "n2", line: 5, archived: true })]}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Note on line 2" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Note on line 5" })).not.toBeInTheDocument();
  });

  it("skips a note whose anchor no longer resolves (no line to sit beside)", () => {
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[note({ id: "n1", line: null, orphaned: true, start: null, end: null })]}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /note on line/i })).not.toBeInTheDocument();
  });

  it("positions a dot where the caller measured its line", () => {
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[note({ id: "n1", line: 2 })]}
        onChanged={vi.fn()}
        topFor={(line) => line * 100}
      />,
    );

    const dot = screen.getByRole("button", { name: "Note on line 2" }).parentElement;
    expect(dot?.style.top).toBe("200px");
  });

  it("falls back to the line slot when the caller cannot measure", () => {
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[note({ id: "n1", line: 2 }), note({ id: "n2", line: 10 })]}
        onChanged={vi.fn()}
        topFor={() => null}
      />,
    );

    const first = screen.getByRole("button", { name: "Note on line 2" }).parentElement;
    const second = screen.getByRole("button", { name: "Note on line 10" }).parentElement;
    expect(Number.parseFloat(first?.style.top ?? "")).toBeGreaterThan(0);
    expect(Number.parseFloat(second?.style.top ?? "")).toBeGreaterThan(
      Number.parseFloat(first?.style.top ?? ""),
    );
  });

  it("positions each dot's slot in increasing order of its line number", () => {
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[note({ id: "n1", line: 2 }), note({ id: "n2", line: 10 })]}
        onChanged={vi.fn()}
      />,
    );

    const first = screen.getByRole("button", { name: "Note on line 2" }).parentElement;
    const second = screen.getByRole("button", { name: "Note on line 10" }).parentElement;
    const firstTop = Number.parseFloat(first?.style.top ?? "");
    const secondTop = Number.parseFloat(second?.style.top ?? "");
    expect(secondTop).toBeGreaterThan(firstTop);
  });

  it("dims a resolved thread's dot, so it reads settled before it's even opened", () => {
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[note({ status: "resolved" })]}
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Note on line 4" })).toHaveClass("bg-muted-soft");
  });

  it("hovering a dot opens its thread, with the note text and the anchored quote", async () => {
    const user = userEvent.setup();
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[note({ orphaned: true })]}
        onChanged={vi.fn()}
      />,
    );

    await user.hover(screen.getByRole("button", { name: "Note on line 4" }));

    const card = screen.getByRole("note");
    expect(card).toHaveTextContent("a note about this");
    expect(card).toHaveTextContent("the quoted span");
    expect(card).toHaveTextContent(/anchor lost/i);
  });

  it("hovering a different dot closes the previous card and opens only the new one", async () => {
    const user = userEvent.setup();
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[
          note({ id: "n1", line: 2, text: "note A" }),
          note({ id: "n2", line: 6, text: "note B" }),
        ]}
        onChanged={vi.fn()}
      />,
    );

    await user.hover(screen.getByRole("button", { name: "Note on line 2" }));
    expect(screen.getByRole("note")).toHaveTextContent("note A");

    await user.hover(screen.getByRole("button", { name: "Note on line 6" }));

    expect(screen.queryByText("note A")).not.toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("note B");
  });

  it("opens a note's thread when requested from outside, then reports it handled", async () => {
    const onRequestOpenHandled = vi.fn();
    const { rerender } = render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[note({ id: "n1", line: 2, text: "note A" })]}
        onChanged={vi.fn()}
        requestOpen={null}
        onRequestOpenHandled={onRequestOpenHandled}
      />,
    );

    expect(screen.queryByRole("note")).not.toBeInTheDocument();

    rerender(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[note({ id: "n1", line: 2, text: "note A" })]}
        onChanged={vi.fn()}
        requestOpen="n1"
        onRequestOpenHandled={onRequestOpenHandled}
      />,
    );

    expect(screen.getByRole("note")).toHaveTextContent("note A");
    expect(onRequestOpenHandled).toHaveBeenCalledTimes(1);
  });

  it("fans out two notes on the same line so both dots stay individually reachable", () => {
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[
          note({ id: "n1", line: 4, text: "note A" }),
          note({ id: "n2", line: 4, text: "note B" }),
        ]}
        onChanged={vi.fn()}
      />,
    );

    const first = screen.getByRole("button", { name: "Note on line 4" }).parentElement;
    const second = screen.getByRole("button", { name: "Note on line 4 (2)" }).parentElement;
    expect(Number.parseFloat(second?.style.top ?? "")).toBeGreaterThan(
      Number.parseFloat(first?.style.top ?? ""),
    );
  });

  it("each dot in a same-line pair still opens its own thread", async () => {
    const user = userEvent.setup();
    render(
      <NoteMargin
        projectId="proj-1"
        path="docs/guide.md"
        notes={[
          note({ id: "n1", line: 4, text: "note A" }),
          note({ id: "n2", line: 4, text: "note B" }),
        ]}
        onChanged={vi.fn()}
      />,
    );

    await user.hover(screen.getByRole("button", { name: "Note on line 4" }));
    expect(screen.getByRole("note")).toHaveTextContent("note A");

    await user.hover(screen.getByRole("button", { name: "Note on line 4 (2)" }));
    expect(screen.getByRole("note")).toHaveTextContent("note B");
  });

  it("resolving from an open dot's thread reports the change", async () => {
    vi.mocked(projectsApi.resolveNote).mockResolvedValue({ ...note(), status: "resolved" });
    const onChanged = vi.fn();
    const user = userEvent.setup();

    render(
      <NoteMargin projectId="proj-1" path="docs/guide.md" notes={[note()]} onChanged={onChanged} />,
    );

    await user.hover(screen.getByRole("button", { name: "Note on line 4" }));
    await user.click(screen.getByRole("button", { name: "Resolve" }));

    await vi.waitFor(() =>
      expect(projectsApi.resolveNote).toHaveBeenCalledWith("proj-1", "note-1", "docs/guide.md"),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("deleting from an open dot's thread, once confirmed, reports the change", async () => {
    vi.mocked(projectsApi.deleteNote).mockResolvedValue(undefined);
    const onChanged = vi.fn();
    const user = userEvent.setup();

    render(
      <NoteMargin projectId="proj-1" path="docs/guide.md" notes={[note()]} onChanged={onChanged} />,
    );

    await user.hover(screen.getByRole("button", { name: "Note on line 4" }));
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));

    await vi.waitFor(() =>
      expect(projectsApi.deleteNote).toHaveBeenCalledWith("proj-1", "note-1", "docs/guide.md"),
    );
    expect(onChanged).toHaveBeenCalledTimes(1);
  });
});
