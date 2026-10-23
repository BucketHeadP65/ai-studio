import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { ArtifactAction, ArtifactRef } from "@/api/studio-types";
import { ArtifactActionSchema, WorkflowPresentationSchema } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { ArtifactActions } from "./artifact-actions";

// The closed intent table: every action a result offers is one of the kinds this app already
// implements, pressed by a person, and carried out through the host's own fixed functions. A
// presentation cannot name a URL, a method or a body, and an intent with no handler installed
// says so rather than inventing a route.

const BASE = "http://studio.test";

const DRAFT: ArtifactRef = {
  id: "draft",
  label: "The draft",
  run_id: "run-1",
  node_id: "draft_report",
  turn: null,
  value_digest: "a".repeat(64),
  media_type: "text/markdown",
};

const SUMMARY: ArtifactRef = {
  ...DRAFT,
  id: "summary",
  label: "The summary",
  node_id: "summarise",
  value_digest: "b".repeat(64),
};

const SAVE_NOTE: ArtifactAction = {
  kind: "save_note",
  id: "keep-the-draft",
  label: "Keep the draft",
  artifact_id: "draft",
  destination: "user_selects",
};

const RECORD_REVIEW: ArtifactAction = {
  kind: "record_owner_review",
  id: "record-my-reading",
  label: "Record my reading",
  artifact_ids: ["draft", "summary"],
};

function resolving(sent: unknown[]): void {
  server.use(
    http.post(`${BASE}/api/runs/history/run-1/artifacts/resolve`, async ({ request }) => {
      const body = await request.json();
      sent.push(body);
      const asked = (body as { ref: ArtifactRef }).ref;
      return HttpResponse.json({ ref: asked, value: `the bytes of ${asked.id}` });
    }),
  );
}

describe("ArtifactActions parsing", () => {
  it("refuses an action carrying an endpoint or a method", () => {
    expect(
      ArtifactActionSchema.safeParse({ ...SAVE_NOTE, endpoint: "https://example.test/keep" })
        .success,
    ).toBe(false);
    expect(ArtifactActionSchema.safeParse({ ...SAVE_NOTE, method: "POST" }).success).toBe(false);
    expect(
      ArtifactActionSchema.safeParse({ ...RECORD_REVIEW, url: "https://example.test/review" })
        .success,
    ).toBe(false);
    expect(
      ArtifactActionSchema.safeParse({ ...RECORD_REVIEW, body: { anything: true } }).success,
    ).toBe(false);
  });

  it("refuses a whole presentation whose action names an intent this app does not implement", () => {
    const parsed = WorkflowPresentationSchema.safeParse({
      actions: [{ kind: "call_webhook", id: "ring-it", label: "Ring it" }],
    });

    expect(parsed.success).toBe(false);
  });

  it("keeps a presentation whose actions are the two intents this app does implement", () => {
    const parsed = WorkflowPresentationSchema.safeParse({ actions: [SAVE_NOTE, RECORD_REVIEW] });

    expect(parsed.success).toBe(true);
  });
});

describe("ArtifactActions with no handler installed", () => {
  it("shows the action as unavailable rather than reaching for a route of its own", async () => {
    const sent: unknown[] = [];
    resolving(sent);

    render(
      <ArtifactActions runId="run-1" actions={[SAVE_NOTE]} artifacts={[DRAFT]} handlers={{}} />,
    );

    const button = screen.getByRole("button", { name: "Keep the draft" });
    expect(button).toBeDisabled();
    expect(screen.getByText(/not available here yet/)).toBeInTheDocument();
    await userEvent.click(button);
    expect(sent).toEqual([]);
  });

  it("shows an action naming an artifact this run never fixed as unavailable", () => {
    render(
      <ArtifactActions
        runId="run-1"
        actions={[SAVE_NOTE]}
        artifacts={[SUMMARY]}
        handlers={{ saveNote: vi.fn() }}
      />,
    );

    expect(screen.getByRole("button", { name: "Keep the draft" })).toBeDisabled();
    expect(screen.getByText(/This run fixed no value/)).toBeInTheDocument();
  });
});

describe("ArtifactActions save_note", () => {
  it("resolves its one artifact, opens the preview, and asks the person where to keep it", async () => {
    const sent: unknown[] = [];
    resolving(sent);
    const saveNote = vi.fn();

    render(
      <ArtifactActions
        runId="run-1"
        actions={[SAVE_NOTE]}
        artifacts={[DRAFT]}
        handlers={{ saveNote }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Keep the draft" }));

    expect(await screen.findByText(/Choose where to keep it/)).toBeInTheDocument();
    expect(sent).toEqual([{ ref: DRAFT }]);
    expect(saveNote).toHaveBeenCalledTimes(1);
    expect(saveNote).toHaveBeenCalledWith({
      actionId: "keep-the-draft",
      destination: "user_selects",
      document: { ref: DRAFT, value: "the bytes of draft" },
    });
  });

  it("does nothing until it is pressed", () => {
    const sent: unknown[] = [];
    resolving(sent);
    const saveNote = vi.fn();

    render(
      <ArtifactActions
        runId="run-1"
        actions={[SAVE_NOTE]}
        artifacts={[DRAFT]}
        handlers={{ saveNote }}
      />,
    );

    expect(saveNote).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it("shows the host's own refusal when the reference cannot be resolved", async () => {
    server.use(
      http.post(`${BASE}/api/runs/history/run-1/artifacts/resolve`, () =>
        HttpResponse.json(
          { ok: false, message: "this run recorded no value under that artifact's source" },
          { status: 404 },
        ),
      ),
    );
    const saveNote = vi.fn();

    render(
      <ArtifactActions
        runId="run-1"
        actions={[SAVE_NOTE]}
        artifacts={[DRAFT]}
        handlers={{ saveNote }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Keep the draft" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "this run recorded no value under that artifact's source",
    );
    expect(saveNote).not.toHaveBeenCalled();
  });
});

describe("ArtifactActions record_owner_review", () => {
  it("resolves every declared artifact into the host's own references before opening the form", async () => {
    const sent: unknown[] = [];
    resolving(sent);
    const recordOwnerReview = vi.fn();

    render(
      <ArtifactActions
        runId="run-1"
        actions={[RECORD_REVIEW]}
        artifacts={[DRAFT, SUMMARY]}
        handlers={{ recordOwnerReview }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Record my reading" }));

    // Announced, like a refusal is: a press that worked is as worth hearing as one that failed.
    expect(await screen.findByRole("status")).toHaveTextContent(/Opened the review form/);
    expect(sent).toEqual([{ ref: DRAFT }, { ref: SUMMARY }]);
    expect(recordOwnerReview).toHaveBeenCalledWith({
      actionId: "record-my-reading",
      refs: [DRAFT, SUMMARY],
      documents: [
        { ref: DRAFT, value: "the bytes of draft" },
        { ref: SUMMARY, value: "the bytes of summary" },
      ],
    });
  });

  it("presses once while the form it opened is still open", async () => {
    const sent: unknown[] = [];
    resolving(sent);
    const recordOwnerReview = vi.fn();
    const panel = (openActionId: string | null) => (
      <ArtifactActions
        runId="run-1"
        actions={[RECORD_REVIEW]}
        artifacts={[DRAFT, SUMMARY]}
        handlers={{ recordOwnerReview }}
        openActionId={openActionId}
      />
    );

    const { rerender } = render(panel(null));
    const button = screen.getByRole("button", { name: "Record my reading" });
    await userEvent.click(button);
    await screen.findByText(/Opened the review form/);
    rerender(panel("record-my-reading"));
    expect(button).toBeDisabled();
    await userEvent.click(button);

    expect(recordOwnerReview).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([{ ref: DRAFT }, { ref: SUMMARY }]);
    expect(screen.getAllByText(/Opened the review form/)).toHaveLength(1);
  });

  it("offers the action again once the form it opened has closed, and resolves afresh", async () => {
    const sent: unknown[] = [];
    resolving(sent);
    const recordOwnerReview = vi.fn();
    const panel = (openActionId: string | null) => (
      <ArtifactActions
        runId="run-1"
        actions={[RECORD_REVIEW]}
        artifacts={[DRAFT, SUMMARY]}
        handlers={{ recordOwnerReview }}
        openActionId={openActionId}
      />
    );

    const { rerender } = render(panel(null));
    await userEvent.click(screen.getByRole("button", { name: "Record my reading" }));
    await screen.findByText(/Opened the review form/);
    rerender(panel("record-my-reading"));
    // Backed out of, refused, or recorded: whatever happened in there, the form is gone.
    rerender(panel(null));

    const button = screen.getByRole("button", { name: "Record my reading" });
    expect(button).toBeEnabled();
    expect(screen.queryByText(/Opened the review form/)).not.toBeInTheDocument();
    await userEvent.click(button);

    expect(recordOwnerReview).toHaveBeenCalledTimes(2);
    expect(sent).toEqual([{ ref: DRAFT }, { ref: SUMMARY }, { ref: DRAFT }, { ref: SUMMARY }]);
  });

  it("leaves one run's refusal behind when another run is opened in its place", async () => {
    server.use(
      http.post(`${BASE}/api/runs/history/run-a/artifacts/resolve`, () =>
        HttpResponse.json(
          { ok: false, message: "this run recorded no value under that artifact's source" },
          { status: 404 },
        ),
      ),
    );
    const recordOwnerReview = vi.fn();
    const panel = (runId: string) => (
      <ArtifactActions
        runId={runId}
        actions={[RECORD_REVIEW]}
        artifacts={[DRAFT, SUMMARY]}
        handlers={{ recordOwnerReview }}
        openActionId={null}
      />
    );

    const { rerender } = render(panel("run-a"));
    await userEvent.click(screen.getByRole("button", { name: "Record my reading" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "this run recorded no value under that artifact's source",
    );

    rerender(panel("run-b"));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record my reading" })).toBeEnabled();
  });

  it("abandons a press whose run the reader has left, opening nothing and saying nothing", async () => {
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const sent: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/runs/history/run-a/artifacts/resolve`, async ({ request }) => {
        const body = await request.json();
        sent.push(body);
        await held;
        const asked = (body as { ref: ArtifactRef }).ref;
        return HttpResponse.json({ ref: asked, value: `the bytes of ${asked.id}` });
      }),
      http.post(`${BASE}/api/runs/history/run-b/artifacts/resolve`, async ({ request }) => {
        const body = await request.json();
        sent.push(body);
        const asked = (body as { ref: ArtifactRef }).ref;
        return HttpResponse.json({ ref: asked, value: `the bytes of ${asked.id}` });
      }),
    );
    const recordOwnerReview = vi.fn();
    const panel = (runId: string) => (
      <ArtifactActions
        runId={runId}
        actions={[RECORD_REVIEW]}
        artifacts={[DRAFT, SUMMARY]}
        handlers={{ recordOwnerReview }}
        openActionId={null}
      />
    );

    const { rerender } = render(panel("run-a"));
    await userEvent.click(screen.getByRole("button", { name: "Record my reading" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    // The reader moves to another run, and only then does the answer come back.
    rerender(panel("run-b"));
    release();

    // A press that does land, under the run that is now open. Anything the abandoned one was
    // going to do, it had every chance to do before this one finished.
    await userEvent.click(screen.getByRole("button", { name: "Record my reading" }));
    expect(await screen.findByText(/Opened the review form/)).toBeInTheDocument();
    expect(recordOwnerReview).toHaveBeenCalledTimes(1);
    expect(recordOwnerReview.mock.calls[0]?.[0]).toMatchObject({ refs: [DRAFT, SUMMARY] });
  });

  it("stays pressable after the host refuses a reference, so the next press resolves it again", async () => {
    let refuse = true;
    const sent: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/runs/history/run-1/artifacts/resolve`, async ({ request }) => {
        const body = await request.json();
        sent.push(body);
        if (refuse) {
          return HttpResponse.json(
            { ok: false, message: "the archived value no longer matches the digest" },
            { status: 409 },
          );
        }
        const asked = (body as { ref: ArtifactRef }).ref;
        return HttpResponse.json({ ref: asked, value: `the bytes of ${asked.id}` });
      }),
    );
    const recordOwnerReview = vi.fn();

    render(
      <ArtifactActions
        runId="run-1"
        actions={[RECORD_REVIEW]}
        artifacts={[DRAFT, SUMMARY]}
        handlers={{ recordOwnerReview }}
        openActionId={null}
      />,
    );
    const button = screen.getByRole("button", { name: "Record my reading" });
    await userEvent.click(button);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "the archived value no longer matches the digest",
    );
    expect(recordOwnerReview).not.toHaveBeenCalled();
    expect(button).toBeEnabled();

    refuse = false;
    await userEvent.click(button);

    expect(await screen.findByText(/Opened the review form/)).toBeInTheDocument();
    expect(recordOwnerReview).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([{ ref: DRAFT }, { ref: DRAFT }, { ref: SUMMARY }]);
  });
});
