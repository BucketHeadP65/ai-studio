import { screen, waitFor, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { ArtifactRef } from "@/api/studio-types";
import type { NoteDraft } from "@/api/types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { SaveArtifactNote } from "./save-artifact-note";

// Keeping a run's proposed note. The surface writes nothing on its own: the person picks the
// destination, reads what it would become there, and presses Keep. What it sends back is exactly
// what the destination rendered, digests and all, so the host is the one that decides whether the
// note is still the note that was read.

const BASE = "http://api.test";
const REGISTRY = `${BASE}/api/knowledge-bases`;
const PREVIEW = `${BASE}/api/knowledge-bases/notebook/notes/preview`;
const KEEP = `${BASE}/api/knowledge-bases/notebook/notes`;

const ORIGIN: ArtifactRef = {
  id: "retention_draft",
  label: "Proposed knowledge note",
  run_id: "run-1",
  node_id: "present",
  turn: null,
  value_digest: "a".repeat(64),
  media_type: "application/json",
};

const PROPOSED: NoteDraft = {
  page_type: "concept",
  title: "What the run concluded",
  summary: "One reading of what the evidence supports.",
  frontmatter: { status: "draft" },
  sections: [{ heading: "TL;DR", body: "It holds under the stated conditions." }],
};

interface Kept {
  idempotency_key: string;
  origin: ArtifactRef;
  draft: NoteDraft;
  schema_digest: string;
  preview_digest: string;
}

function previewOf(draft: NoteDraft, digest: string) {
  return {
    knowledge_base: "notebook",
    note_id: "c-0123456789abcdef01234567",
    draft: { ...draft, frontmatter: { ...draft.frontmatter, created: "2026-09-13" } },
    markdown: `# ${draft.title}\n`,
    path: "Wiki/Concepts/0123456789abcdef01234567.md",
    read_ref: "page:c-0123456789abcdef01234567",
    schema_digest: "s".repeat(64),
    preview_digest: digest,
    warnings: [],
  };
}

// One registry row, spelled in full: the list schema is strict, mirroring what the host emits.
function row(name: string, indexed: boolean) {
  return {
    name,
    registered: true,
    indexed,
    broken: false,
    path: `/knowledge-bases/${name}`,
    description: "",
    pages: null,
    page_types: [],
    last_indexed: null,
  };
}

function registry(): void {
  server.use(
    http.get(REGISTRY, () =>
      HttpResponse.json({ knowledge_bases: [row("notebook", true), row("elsewhere", false)] }),
    ),
  );
}

// A destination that renders whatever it is handed, and keeps whatever it is sent.
function destination(sent: Kept[], outcome = "saved"): void {
  registry();
  server.use(
    http.post(PREVIEW, async ({ request }) => {
      const body = (await request.json()) as { origin: ArtifactRef; edited_draft?: NoteDraft };
      const draft = body.edited_draft ?? PROPOSED;
      return HttpResponse.json(previewOf(draft, `p${draft.title.length}`.padEnd(64, "0")));
    }),
    http.post(KEEP, async ({ request }) => {
      const body = (await request.json()) as Kept;
      sent.push(body);
      return HttpResponse.json({
        outcome,
        knowledge_base: "notebook",
        note_id: "c-0123456789abcdef01234567",
        path: "Wiki/Concepts/0123456789abcdef01234567.md",
        read_ref: "page:c-0123456789abcdef01234567",
        detail: "",
        warnings: [],
      });
    }),
  );
}

async function chooseNotebook(): Promise<void> {
  await waitFor(() =>
    expect(
      within(screen.getByLabelText("Knowledge base")).getByRole("option", { name: "notebook" }),
    ).toBeInTheDocument(),
  );
  await userEvent.selectOptions(screen.getByLabelText("Knowledge base"), "notebook");
  await screen.findByText("Wiki/Concepts/0123456789abcdef01234567.md");
}

describe("SaveArtifactNote", () => {
  it("writes nothing until a destination is chosen and the note is kept", async () => {
    const sent: Kept[] = [];
    destination(sent);
    render(<SaveArtifactNote origin={ORIGIN} proposed={PROPOSED} onClose={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Keep" })).toBeDisabled();
    expect(sent).toHaveLength(0);
  });

  it("shows where the note would land once a destination is chosen", async () => {
    destination([]);
    render(<SaveArtifactNote origin={ORIGIN} proposed={PROPOSED} onClose={vi.fn()} />);

    await chooseNotebook();
    expect(screen.getByRole("button", { name: "Keep" })).toBeEnabled();
  });

  it("sends back exactly what the destination rendered, digests and all", async () => {
    const sent: Kept[] = [];
    destination(sent);
    render(<SaveArtifactNote origin={ORIGIN} proposed={PROPOSED} onClose={vi.fn()} />);

    await chooseNotebook();
    await userEvent.click(screen.getByRole("button", { name: "Keep" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      origin: ORIGIN,
      schema_digest: "s".repeat(64),
      draft: { title: "What the run concluded", frontmatter: { created: "2026-09-13" } },
    });
    expect(sent[0]?.idempotency_key).not.toBe("");
    expect(await screen.findByRole("status")).toHaveTextContent("Kept, and indexed.");
  });

  it("will not keep a note the destination has not read, and offers the preview again", async () => {
    const sent: Kept[] = [];
    destination(sent);
    render(<SaveArtifactNote origin={ORIGIN} proposed={PROPOSED} onClose={vi.fn()} />);

    await chooseNotebook();
    await userEvent.type(screen.getByLabelText("Title"), "!");

    expect(screen.getByRole("button", { name: "Keep" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("changed the note since it was read");
    expect(sent).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: "Preview again" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Keep" })).toBeEnabled());
  });

  it("keeps the edited words when the person edited them", async () => {
    const sent: Kept[] = [];
    destination(sent);
    render(<SaveArtifactNote origin={ORIGIN} proposed={PROPOSED} onClose={vi.fn()} />);

    await chooseNotebook();
    await userEvent.clear(screen.getByLabelText("Summary"));
    await userEvent.type(screen.getByLabelText("Summary"), "My own words.");
    await userEvent.click(screen.getByRole("button", { name: "Preview again" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Keep" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Keep" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.draft.summary).toBe("My own words.");
  });

  it("says a note was kept but not indexed, and offers to try indexing again under the same request", async () => {
    const sent: Kept[] = [];
    destination(sent, "saved_not_indexed");
    render(<SaveArtifactNote origin={ORIGIN} proposed={PROPOSED} onClose={vi.fn()} />);

    await chooseNotebook();
    await userEvent.click(screen.getByRole("button", { name: "Keep" }));

    expect(await screen.findByRole("status")).toHaveTextContent("not indexed yet");
    const retry = await screen.findByRole("button", { name: "Try indexing again" });
    await userEvent.click(retry);

    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1]?.idempotency_key).toBe(sent[0]?.idempotency_key);
    expect(sent[1]?.preview_digest).toBe(sent[0]?.preview_digest);
    expect(sent[1]?.draft).toEqual(sent[0]?.draft);
  });

  it("shows the host's own words when it refuses to keep the note", async () => {
    registry();
    server.use(
      http.post(PREVIEW, () => HttpResponse.json(previewOf(PROPOSED, "p".repeat(64)))),
      http.post(KEEP, () =>
        HttpResponse.json(
          {
            error: "target_changed",
            detail: "the file at this path holds something other than this note",
            outcome: "changed",
            knowledge_base: "notebook",
            note_id: "c-0123456789abcdef01234567",
            path: "Wiki/Concepts/0123456789abcdef01234567.md",
            read_ref: "page:c-0123456789abcdef01234567",
            detail_text: "",
            warnings: [],
          },
          { status: 409 },
        ),
      ),
    );
    render(<SaveArtifactNote origin={ORIGIN} proposed={PROPOSED} onClose={vi.fn()} />);

    await chooseNotebook();
    await userEvent.click(screen.getByRole("button", { name: "Keep" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "holds something other than this note",
    );
  });

  it("gives a freshly previewed note its own request id, so editing after a partial keep is not refused", async () => {
    const sent: Kept[] = [];
    destination(sent, "saved_not_indexed");
    render(<SaveArtifactNote origin={ORIGIN} proposed={PROPOSED} onClose={vi.fn()} />);

    await chooseNotebook();
    await userEvent.click(screen.getByRole("button", { name: "Keep" }));
    expect(await screen.findByRole("status")).toHaveTextContent("not indexed yet");

    await userEvent.clear(screen.getByLabelText("Summary"));
    await userEvent.type(screen.getByLabelText("Summary"), "Corrected.");
    await userEvent.click(screen.getByRole("button", { name: "Preview again" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Keep" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Keep" }));

    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1]?.draft.summary).toBe("Corrected.");
    expect(sent[1]?.idempotency_key).not.toBe(sent[0]?.idempotency_key);
  });

  it("gives every section its own field even when two of them share a heading", async () => {
    const twins: NoteDraft = {
      ...PROPOSED,
      sections: [
        { heading: "Evidence", body: "What supports it." },
        { heading: "Evidence", body: "What argues the other way." },
      ],
    };
    const sent: Kept[] = [];
    destination(sent);
    server.use(
      http.post(PREVIEW, async ({ request }) => {
        const body = (await request.json()) as { edited_draft?: NoteDraft };
        return HttpResponse.json(previewOf(body.edited_draft ?? twins, "p".repeat(64)));
      }),
    );
    render(<SaveArtifactNote origin={ORIGIN} proposed={twins} onClose={vi.fn()} />);

    await chooseNotebook();
    const fields = screen.getAllByLabelText("Evidence");
    expect(fields).toHaveLength(2);
    expect(fields[0]).toHaveValue("What supports it.");
    expect(fields[1]).toHaveValue("What argues the other way.");
  });

  it("starts over when the destination is changed", async () => {
    const sent: Kept[] = [];
    destination(sent);
    render(<SaveArtifactNote origin={ORIGIN} proposed={PROPOSED} onClose={vi.fn()} />);

    await chooseNotebook();
    await userEvent.type(screen.getByLabelText("Title"), "!");
    expect(screen.getByRole("button", { name: "Keep" })).toBeDisabled();

    await userEvent.selectOptions(screen.getByLabelText("Knowledge base"), "");
    expect(screen.getByLabelText("Title")).toHaveValue("What the run concluded");
    expect(sent).toHaveLength(0);
  });
});
