import { cleanup, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

import { CreateKnowledgeBaseDialog } from "./create-dialog";

const BASE = "http://api.test";

const TEMPLATES = [{ slug: "widgets", title: "Widgets template", path: "/templates/widgets" }];

function mockTemplates(templates: typeof TEMPLATES = TEMPLATES) {
  server.use(http.get(`${BASE}/api/templates`, () => HttpResponse.json({ templates })));
}

beforeEach(() => mockTemplates());
afterEach(cleanup);

function setup(overrides: Partial<Parameters<typeof CreateKnowledgeBaseDialog>[0]> = {}) {
  const onCreate = vi.fn();
  const onCancel = vi.fn();
  const onDesignWithAI = vi.fn();
  render(
    <CreateKnowledgeBaseDialog
      open
      creating={false}
      onCreate={onCreate}
      onCancel={onCancel}
      onDesignWithAI={onDesignWithAI}
      {...overrides}
    />,
  );
  return { onCreate, onCancel, onDesignWithAI };
}

describe("CreateKnowledgeBaseDialog", () => {
  it("renders nothing when closed", () => {
    render(
      <CreateKnowledgeBaseDialog
        open={false}
        creating={false}
        onCreate={vi.fn()}
        onCancel={vi.fn()}
        onDesignWithAI={vi.fn()}
      />,
    );

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens with Minimal selected and the AI button disabled until a path is entered", () => {
    setup();

    expect(screen.getByRole("radio", { name: "Minimal" })).toBeChecked();
    const aiButton = screen.getByRole("button", { name: "Design it with AI" });
    expect(aiButton).toBeDisabled();
    expect(aiButton).toHaveAttribute("title", "Enter a path first");
  });

  it("enables the AI button once a path is entered", async () => {
    setup();

    await userEvent.type(screen.getByLabelText("Path"), "/knowledge-bases/scratch");
    const aiButton = screen.getByRole("button", { name: "Design it with AI" });
    expect(aiButton).toBeEnabled();
  });

  it("lists a template from the catalog as a radio option", async () => {
    setup();

    expect(await screen.findByRole("radio", { name: "Widgets template" })).toBeInTheDocument();
  });

  it("builds a template payload when a template radio is picked, for Create", async () => {
    const { onCreate } = setup();

    await userEvent.type(screen.getByLabelText("Name"), "my-widgets");
    await userEvent.type(screen.getByLabelText("Path"), "/knowledge-bases/my-widgets");
    await userEvent.click(await screen.findByRole("radio", { name: "Widgets template" }));
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(onCreate).toHaveBeenCalledWith({
      name: "my-widgets",
      path: "/knowledge-bases/my-widgets",
      template: "widgets",
    });
  });

  it("builds the same template payload Create would and hands it to onDesignWithAI instead", async () => {
    const { onDesignWithAI, onCreate } = setup();

    await userEvent.type(screen.getByLabelText("Name"), "my-widgets");
    await userEvent.type(screen.getByLabelText("Path"), "/knowledge-bases/my-widgets");
    await userEvent.click(await screen.findByRole("radio", { name: "Widgets template" }));
    await userEvent.click(screen.getByRole("button", { name: "Design it with AI" }));

    expect(onDesignWithAI).toHaveBeenCalledWith({
      name: "my-widgets",
      path: "/knowledge-bases/my-widgets",
      template: "widgets",
    });
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("reports the missing name/path via the same form error, without calling onDesignWithAI", async () => {
    const { onDesignWithAI } = setup();

    await userEvent.type(screen.getByLabelText("Path"), "/knowledge-bases/scratch");
    await userEvent.click(screen.getByRole("button", { name: "Design it with AI" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Name and path are both required.");
    expect(onDesignWithAI).not.toHaveBeenCalled();
  });

  it("refuses to submit without a name and path", async () => {
    const { onCreate } = setup();

    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Name and path are both required.");
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("builds the minimal-template schema by default, stamping slug and retrieval.collection from the name", async () => {
    const { onCreate } = setup();

    await userEvent.type(screen.getByLabelText("Name"), "scratch");
    await userEvent.type(screen.getByLabelText("Path"), "/knowledge-bases/scratch");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(onCreate).toHaveBeenCalledWith({
      name: "scratch",
      path: "/knowledge-bases/scratch",
      schema: expect.objectContaining({
        slug: "scratch",
        retrieval: { collection: "scratch" },
        page_types: [{ page_type: "note", id_prefix: "note" }],
      }),
    });
  });

  it("sends the pasted JSON verbatim, without stamping name/retrieval onto it", async () => {
    const { onCreate } = setup();

    await userEvent.type(screen.getByLabelText("Name"), "legal");
    await userEvent.type(screen.getByLabelText("Path"), "/knowledge-bases/legal");
    await userEvent.click(screen.getByRole("radio", { name: "Paste a knowledge base schema JSON" }));

    // userEvent.type interprets `{`/`}` as special keys, so a JSON payload is pasted via
    // fireEvent.change instead of keystroke-by-keystroke typing.
    fireEvent.change(screen.getByLabelText("Knowledge base schema JSON"), {
      target: {
        value: JSON.stringify({
          slug: "legal-eu",
          page_types: [{ page_type: "clause" }],
          retrieval: { collection: "legal-eu" },
        }),
      },
    });

    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(onCreate).toHaveBeenCalledWith({
      name: "legal",
      path: "/knowledge-bases/legal",
      schema: expect.objectContaining({
        slug: "legal-eu",
        retrieval: { collection: "legal-eu" },
        page_types: [{ page_type: "clause" }],
      }),
    });
  });

  it("reports invalid pasted JSON without calling onCreate", async () => {
    const { onCreate } = setup();

    await userEvent.type(screen.getByLabelText("Name"), "legal");
    await userEvent.type(screen.getByLabelText("Path"), "/knowledge-bases/legal");
    await userEvent.click(screen.getByRole("radio", { name: "Paste a knowledge base schema JSON" }));
    await userEvent.type(screen.getByLabelText("Knowledge base schema JSON"), "not json");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(screen.getByRole("alert")).toHaveTextContent(/Invalid JSON/);
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("reports a pasted schema with an empty page_types without calling onCreate", async () => {
    const { onCreate } = setup();

    await userEvent.type(screen.getByLabelText("Name"), "legal");
    await userEvent.type(screen.getByLabelText("Path"), "/knowledge-bases/legal");
    await userEvent.click(screen.getByRole("radio", { name: "Paste a knowledge base schema JSON" }));
    fireEvent.change(screen.getByLabelText("Knowledge base schema JSON"), {
      target: { value: JSON.stringify({ slug: "legal", page_types: [] }) },
    });
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(screen.getByRole("alert")).toHaveTextContent(/page type/);
    expect(onCreate).not.toHaveBeenCalled();
  });

  it("cancels via the Cancel button and via Escape", async () => {
    const { onCancel } = setup();

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);

    await userEvent.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it("disables Create/Cancel and relabels the button while creating", () => {
    setup({ creating: true });

    expect(screen.getByRole("button", { name: "Creating..." })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  });

  it("shows a server-side error alongside any form error", () => {
    setup({ error: "a knowledge base named 'inbox' is already registered" });

    expect(screen.getByRole("alert")).toHaveTextContent("already registered");
  });

  it("still allows Minimal when the template catalog fails to load", async () => {
    server.use(
      http.get(`${BASE}/api/templates`, () =>
        HttpResponse.json({ error: "boom", detail: "templates are unreachable" }, { status: 500 }),
      ),
    );
    const { onCreate } = setup();

    expect(await screen.findByText(/Could not load templates/)).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Name"), "scratch");
    await userEvent.type(screen.getByLabelText("Path"), "/knowledge-bases/scratch");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(onCreate).toHaveBeenCalledWith({
      name: "scratch",
      path: "/knowledge-bases/scratch",
      schema: expect.objectContaining({ slug: "scratch", page_types: [{ page_type: "note", id_prefix: "note" }] }),
    });
  });
});
