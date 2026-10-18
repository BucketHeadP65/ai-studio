import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "client-core/api/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { projectsApi } from "@/api/projects";
import { render } from "@/test-support/render";

import { AddNoteButton, NotePopover, anchorFor } from "./note-popover";

vi.mock("@/api/projects", () => ({
  projectsApi: {
    createNote: vi.fn(),
  },
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("anchorFor", () => {
  it("slices exact/prefix/suffix from the source, clamping the prefix at the file start", () => {
    const source =
      "# Title\n\nBody text that continues on for a while to have enough characters for context slicing tests.";
    const start = source.indexOf("Body");
    const end = start + "Body".length;

    expect(anchorFor(source, start, end)).toEqual({
      exact: "Body",
      prefix: "# Title\n\n",
      suffix: " text that continues on for a wh",
    });
  });

  it("maps a selection across a heading boundary", () => {
    const source =
      "# Getting started\n\nRead this whole paragraph before doing anything else at all today.\n";
    const start = source.indexOf("started");
    const end = start + "started".length;

    expect(anchorFor(source, start, end)).toEqual({
      exact: "started",
      prefix: "# Getting ",
      suffix: "\n\nRead this whole paragraph befo",
    });
  });

  it("maps a selection inside a list item", () => {
    const source =
      "Notes:\n\n- alpha item description one\n- beta item description two\n- gamma item description three\n";
    const start = source.indexOf("item description two");
    const end = start + "item description two".length;

    expect(anchorFor(source, start, end)).toEqual({
      exact: "item description two",
      prefix: "pha item description one\n- beta ",
      suffix: "\n- gamma item description three\n",
    });
  });

  it("clamps the suffix at the file end", () => {
    const source =
      "Some long lead-in paragraph text that goes on for quite a while before we reach the final word here.";
    const start = source.indexOf("final word here");
    const end = source.length;

    expect(anchorFor(source, start, end)).toEqual({
      exact: "final word here.",
      prefix: "ite a while before we reach the ",
      suffix: "",
    });
  });
});

describe("<AddNoteButton />", () => {
  it("renders at the given position and calls onClick", async () => {
    const onClick = vi.fn();
    const user = userEvent.setup();

    render(<AddNoteButton position={{ top: 30, left: 40 }} onClick={onClick} />);

    const button = screen.getByRole("button", { name: "Add note" });
    expect(button).toHaveStyle({ top: "30px", left: "40px" });

    await user.click(button);

    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("prevents the default mousedown action, so pressing it never collapses the selection", () => {
    render(<AddNoteButton position={{ top: 0, left: 0 }} onClick={vi.fn()} />);

    const event = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    const button = screen.getByRole("button", { name: "Add note" });
    const prevented = !button.dispatchEvent(event);

    expect(prevented).toBe(true);
  });
});

describe("<NotePopover />", () => {
  it("renders at the given position with Save disabled until text is typed", () => {
    render(
      <NotePopover
        projectId="proj-1"
        path="docs/guide.md"
        source="Hello world"
        start={0}
        end={5}
        position={{ top: 120, left: 240 }}
        onSaved={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "New note" });
    expect(dialog).toHaveStyle({ top: "120px", left: "240px" });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("Save posts the exact {path, anchor, text} payload and reports success", async () => {
    vi.mocked(projectsApi.createNote).mockResolvedValue({
      id: "note-1",
      anchor: { exact: "world", prefix: "Hello ", suffix: "" },
      text: "a note",
      created_at: "2026-08-23T00:00:00Z",
      archived: false,
      replies: [],
      status: "open",
    });
    const onSaved = vi.fn();
    const user = userEvent.setup();

    render(
      <NotePopover
        projectId="proj-1"
        path="docs/guide.md"
        source="Hello world"
        start={6}
        end={11}
        position={{ top: 0, left: 0 }}
        onSaved={onSaved}
        onCancel={vi.fn()}
      />,
    );

    await user.type(screen.getByRole("textbox"), "a note");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(projectsApi.createNote).toHaveBeenCalledWith(
        "proj-1",
        "docs/guide.md",
        { exact: "world", prefix: "Hello ", suffix: "" },
        "a note",
      ),
    );
    expect(onSaved).toHaveBeenCalledTimes(1);
  });

  it("a 4xx renders inline in the popover, without closing or reporting success", async () => {
    vi.mocked(projectsApi.createNote).mockRejectedValue(
      new ApiError(400, JSON.stringify({ detail: "text is required" })),
    );
    const onSaved = vi.fn();
    const user = userEvent.setup();

    render(
      <NotePopover
        projectId="proj-1"
        path="docs/guide.md"
        source="Hello world"
        start={0}
        end={5}
        position={{ top: 0, left: 0 }}
        onSaved={onSaved}
        onCancel={vi.fn()}
      />,
    );

    await user.type(screen.getByRole("textbox"), "oops");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("text is required");
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("Cancel reports cancellation without posting", async () => {
    const onCancel = vi.fn();
    const user = userEvent.setup();

    render(
      <NotePopover
        projectId="proj-1"
        path="docs/guide.md"
        source="Hello world"
        start={0}
        end={5}
        position={{ top: 0, left: 0 }}
        onSaved={vi.fn()}
        onCancel={onCancel}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(projectsApi.createNote).not.toHaveBeenCalled();
  });
});
