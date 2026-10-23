import type { ComponentProps } from "react";

import { EditorView } from "@codemirror/view";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, projectsApi } from "@/api/projects";
import { render } from "@/shared/test-support/render";

import { MarkdownEditor } from "./markdown-editor";

vi.mock("@/api/projects", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/projects")>();
  return {
    ...actual,
    projectsApi: {
      ...actual.projectsApi,
      writeFile: vi.fn(),
      readFile: vi.fn(),
    },
  };
});

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
    item: () => null,
    length: 0,
    [Symbol.iterator]: Array.prototype[Symbol.iterator] as unknown as () => Iterator<DOMRect>,
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

function renderEditor(overrides: Partial<ComponentProps<typeof MarkdownEditor>> = {}) {
  const props: ComponentProps<typeof MarkdownEditor> = {
    projectId: "proj-1",
    path: "notes/plan.md",
    initialContent: "# Plan\n\nDraft text.",
    hash: "hash-1",
    obsidian: false,
    absolutePath: "/vault/notes/plan.md",
    onSaved: vi.fn(),
    onToggleView: vi.fn(),
    ...overrides,
  };
  const result = render(<MarkdownEditor {...props} />);
  return { ...result, props };
}

// Edits the mounted buffer through CodeMirror's own API: jsdom implements no
// contenteditable, so typing into `.cm-content` never reaches the document.
function bufferView(): EditorView {
  const dom = document.querySelector<HTMLElement>(".cm-editor");
  if (!dom) throw new Error("expected a mounted CodeMirror editor");
  const view = EditorView.findFromDOM(dom);
  if (!view) throw new Error("expected a CodeMirror view behind the editor DOM");
  return view;
}

function editBuffer(change: { from: number; to?: number; insert?: string }): void {
  act(() => {
    bufferView().dispatch({ changes: change });
  });
}

function appendToBuffer(text: string): void {
  editBuffer({ from: bufferView().state.doc.length, insert: text });
}

beforeEach(() => {
  stubLayoutMeasurement();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  delete document.documentElement.dataset.theme;
});

describe("<MarkdownEditor />", () => {
  it("saves the current buffer with the expected hash", async () => {
    vi.mocked(projectsApi.writeFile).mockResolvedValue("hash-2");
    const onSaved = vi.fn();
    const user = userEvent.setup();

    renderEditor({ onSaved });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(projectsApi.writeFile).toHaveBeenCalledWith(
        "proj-1",
        "notes/plan.md",
        "# Plan\n\nDraft text.",
        "hash-1",
      ),
    );
    await vi.waitFor(() => expect(onSaved).toHaveBeenCalledWith("hash-2"));
  });

  it("saves on Ctrl/Cmd+S", async () => {
    vi.mocked(projectsApi.writeFile).mockResolvedValue("hash-2");

    renderEditor();
    fireEvent.keyDown(screen.getByTestId("markdown-editor"), { key: "s", ctrlKey: true });

    await vi.waitFor(() => expect(projectsApi.writeFile).toHaveBeenCalledTimes(1));
  });

  it("shows an inline conflict bar (not a toast) on a 409, and Reload replaces the buffer and hash", async () => {
    vi.mocked(projectsApi.writeFile).mockRejectedValue(new ApiError(409, "stale"));
    vi.mocked(projectsApi.readFile).mockResolvedValue({
      content: "# Plan\n\nSomeone else's edit.",
      hash: "hash-3",
      obsidian: false,
    });
    const onSaved = vi.fn();
    const user = userEvent.setup();

    renderEditor({ onSaved });
    await user.click(screen.getByRole("button", { name: "Save" }));

    const bar = await screen.findByRole("alert");
    expect(bar).toHaveTextContent(/changed on disk/i);
    expect(screen.queryByTestId("toast")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reload" }));

    await vi.waitFor(() =>
      expect(projectsApi.readFile).toHaveBeenCalledWith("proj-1", "notes/plan.md"),
    );
    await vi.waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());

    // A save after reload carries the freshly-loaded hash, proving it replaced hash-1.
    vi.mocked(projectsApi.writeFile).mockResolvedValue("hash-4");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await vi.waitFor(() =>
      expect(projectsApi.writeFile).toHaveBeenCalledWith(
        "proj-1",
        "notes/plan.md",
        "# Plan\n\nSomeone else's edit.",
        "hash-3",
      ),
    );
  });

  it("surfaces a non-conflict save failure inline, distinct from the conflict bar", async () => {
    vi.mocked(projectsApi.writeFile).mockRejectedValue(new ApiError(500, "boom"));
    const user = userEvent.setup();

    renderEditor();
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(screen.queryByRole("button", { name: "Reload" })).not.toBeInTheDocument();
  });

  it("calls onToggleView from the mode toggle", async () => {
    const onToggleView = vi.fn();
    const user = userEvent.setup();

    renderEditor({ onToggleView });
    await user.click(screen.getByRole("button", { name: "Read" }));

    expect(onToggleView).toHaveBeenCalledTimes(1);
  });

  it("renders the Obsidian button only when obsidian is true, linking the encoded absolute path", () => {
    renderEditor({ obsidian: true, absolutePath: "/vault/notes/plan.md" });

    expect(screen.getByRole("link", { name: /obsidian/i })).toHaveAttribute(
      "href",
      "obsidian://open?path=%2Fvault%2Fnotes%2Fplan.md",
    );
  });

  it("omits the Obsidian button when obsidian is false", () => {
    renderEditor({ obsidian: false });

    expect(screen.queryByRole("link", { name: /obsidian/i })).not.toBeInTheDocument();
  });

  it("writes to the project it was given, not to whichever project is active", async () => {
    vi.mocked(projectsApi.writeFile).mockResolvedValue("hash-2");
    const user = userEvent.setup();

    renderEditor({ projectId: "proj-other" });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(projectsApi.writeFile).toHaveBeenCalledWith(
        "proj-other",
        "notes/plan.md",
        "# Plan\n\nDraft text.",
        "hash-1",
      ),
    );
  });

  it("bridges the app's dark html theme onto its own prose root", async () => {
    document.documentElement.dataset.theme = "factory";

    renderEditor();

    expect(screen.getByTestId("markdown-editor")).toHaveAttribute("data-theme", "dark");

    delete document.documentElement.dataset.theme;
    await vi.waitFor(() =>
      expect(screen.getByTestId("markdown-editor")).not.toHaveAttribute("data-theme"),
    );
  });

  describe("dirtiness", () => {
    it("reports the buffer clean on mount and dirty on the first edit", () => {
      const onDirtyChange = vi.fn();

      renderEditor({ onDirtyChange });
      expect(onDirtyChange).toHaveBeenLastCalledWith(false);

      appendToBuffer(" more");

      expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    });

    it("reports clean again once an edit is typed away", () => {
      const onDirtyChange = vi.fn();

      renderEditor({ onDirtyChange, initialContent: "abc" });
      appendToBuffer("d");
      expect(onDirtyChange).toHaveBeenLastCalledWith(true);

      editBuffer({ from: 3, to: 4, insert: "" });

      expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    });

    it("reports clean after a successful save", async () => {
      vi.mocked(projectsApi.writeFile).mockResolvedValue("hash-2");
      const onDirtyChange = vi.fn();
      const user = userEvent.setup();

      renderEditor({ onDirtyChange });
      appendToBuffer(" more");
      expect(onDirtyChange).toHaveBeenLastCalledWith(true);

      await user.click(screen.getByRole("button", { name: "Save" }));

      await vi.waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
    });

    it("reports clean after reloading the file from disk", async () => {
      vi.mocked(projectsApi.writeFile).mockRejectedValue(new ApiError(409, "stale"));
      vi.mocked(projectsApi.readFile).mockResolvedValue({
        content: "# Plan\n\nSomeone else's edit.",
        hash: "hash-3",
        obsidian: false,
      });
      const onDirtyChange = vi.fn();
      const user = userEvent.setup();

      renderEditor({ onDirtyChange });
      appendToBuffer(" more");
      await user.click(screen.getByRole("button", { name: "Save" }));
      await screen.findByRole("button", { name: "Reload" });

      await user.click(screen.getByRole("button", { name: "Reload" }));

      await vi.waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
    });
  });
});
