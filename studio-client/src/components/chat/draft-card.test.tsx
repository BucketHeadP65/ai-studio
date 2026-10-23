import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { ChatReply, Readiness } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { DraftCard } from "./draft-card";

const BASE = "http://studio.test";

const DESCRIBED: Readiness = { executable: true, described: true, findings: [] };

const REPLY: ChatReply = {
  message: "Drafted lit-check.",
  draft: { spec_yaml: "", title: "lit-check", summary: "", applicability: "" },
  issues: [],
  valid: true,
  intent_issues: [],
  readiness: DESCRIBED,
  graph: {
    name: "lit-check",
    version: 1,
    nodes: [
      {
        key: "a",
        id: "a",
        kind: "run",
        signature: "",
        detail: "",
        tools: [],
        required: true,
        is_output: false,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
      },
      {
        key: "b",
        id: "b",
        kind: "run",
        signature: "",
        detail: "",
        tools: [],
        required: true,
        is_output: false,
        unknown: false,
        brought: false,
        briefing: "",
        input_schemas: {},
        output_schema: null,
        source: "",
      },
    ],
    edges: [],
  },
};

function renderCard(overrides: Partial<Parameters<typeof DraftCard>[0]> = {}) {
  return render(
    <DraftCard
      reply={REPLY}
      sessionId="s1"
      gateMode="wait"
      hasLiveRun={false}
      onChangeGates={vi.fn()}
      {...overrides}
    />,
  );
}

describe("DraftCard", () => {
  it("names the draft and shows it is ready", () => {
    renderCard();
    expect(screen.getByText("lit-check")).toBeInTheDocument();
    expect(screen.getByText("Ready to run")).toBeInTheDocument();
  });

  it("draws the graph with nothing run yet", () => {
    renderCard();
    expect(screen.getByText("a")).toBeInTheDocument();
    expect(screen.getByText("b")).toBeInTheDocument();
  });

  it("says it is not ready to run, and never shows the validator's own words", () => {
    renderCard({
      reply: {
        ...REPLY,
        valid: false,
        issues: [
          "[schema] nodes.2.then: work 'digest_source' returns Digest, but 'flag_ideas' expects list[str]",
        ],
      },
    });
    expect(screen.queryByText("Ready to run")).not.toBeInTheDocument();
    expect(screen.getByText("Not ready to run")).toBeInTheDocument();
    expect(screen.queryByText(/nodes\.2\.then/)).not.toBeInTheDocument();
    expect(screen.queryByText(/list\[str\]/)).not.toBeInTheDocument();
  });

  it("disables Start this run while the draft is not valid", () => {
    renderCard({ reply: { ...REPLY, valid: false, issues: ["missing input"] } });
    expect(screen.getByRole("button", { name: "Start this run" })).toBeDisabled();
  });

  it("disables Start this run once this session already has a live run", () => {
    renderCard({ hasLiveRun: true });
    expect(screen.getByRole("button", { name: "Start this run" })).toBeDisabled();
  });

  it("saves the draft then runs the saved id with the given gate mode", async () => {
    let savedSessionId: string | null = null;
    let runPosted: unknown = null;
    server.use(
      http.post(`${BASE}/api/sessions/:id/save`, ({ params }) => {
        savedSessionId = String(params.id);
        return HttpResponse.json({ ok: true, id: "wf.lit-check.v1" });
      }),
      http.post(`${BASE}/api/sessions/:id/run`, async ({ request }) => {
        runPosted = await request.json();
        return HttpResponse.json({ ok: true, running: "wf.lit-check.v1" });
      }),
    );
    renderCard({ gateMode: "orchestrate" });

    await userEvent.click(screen.getByRole("button", { name: "Start this run" }));

    await waitFor(() => expect(savedSessionId).toBe("s1"));
    await waitFor(() =>
      expect(runPosted).toEqual({
        workflow_id: "wf.lit-check.v1",
        arguments: {},
        gate_mode: "orchestrate",
      }),
    );
    expect(await screen.findByRole("button", { name: "Start this run" })).toBeDisabled();
  });

  it("shows the refusal when starting the run fails", async () => {
    server.use(
      http.post(`${BASE}/api/sessions/:id/save`, () =>
        HttpResponse.json({ detail: "the draft does not validate" }, { status: 400 }),
      ),
    );
    renderCard();

    await userEvent.click(screen.getByRole("button", { name: "Start this run" }));

    expect(await screen.findByText("the draft does not validate")).toBeInTheDocument();
  });

  it("calls onChangeGates from its own button, without starting anything", async () => {
    const onChangeGates = vi.fn();
    renderCard({ onChangeGates });

    await userEvent.click(screen.getByRole("button", { name: "Change the gates" }));

    expect(onChangeGates).toHaveBeenCalledTimes(1);
  });

  it("uses only the house's allowed punctuation", () => {
    renderCard({ reply: { ...REPLY, valid: false, issues: ["draft: unknown input"] } });
    const ALLOWED_PUNCTUATION = /^[\w\s,.?()]*$/;
    for (const text of ["Start this run", "Change the gates"]) {
      expect(screen.getByRole("button", { name: text }).textContent ?? "").toMatch(
        ALLOWED_PUNCTUATION,
      );
    }
  });
});

describe("DraftCard readiness", () => {
  it("will not let a draft nobody described be published", () => {
    renderCard({
      reply: {
        ...REPLY,
        readiness: {
          executable: true,
          described: false,
          findings: [
            { field: "purpose", severity: "error", message: "Say what this workflow is for." },
          ],
        },
      },
    });

    expect(screen.getByRole("button", { name: "Start this run" })).toBeDisabled();
    expect(screen.getByText("Say what this workflow is for.")).toBeInTheDocument();
  });

  it("names every field the backend named, and nothing of its own", () => {
    renderCard({
      reply: {
        ...REPLY,
        readiness: {
          executable: true,
          described: false,
          findings: [
            { field: "title", severity: "error", message: "A title says what this workflow is called. Write one." },
            { field: "not_for", severity: "error", message: "Say what to reach for something else for." },
          ],
        },
      },
    });

    expect(screen.getByText("A title says what this workflow is called. Write one.")).toBeInTheDocument();
    expect(screen.getByText("Say what to reach for something else for.")).toBeInTheDocument();
  });

  it("lets a warning through: it is advice, not a refusal", () => {
    renderCard({
      reply: {
        ...REPLY,
        readiness: {
          executable: false,
          described: true,
          findings: [
            { field: "work", severity: "warning", message: "Nothing here can build publish, so this run cannot execute it." },
          ],
        },
      },
    });

    expect(screen.getByRole("button", { name: "Start this run" })).toBeEnabled();
    expect(
      screen.getByText("Nothing here can build publish, so this run cannot execute it."),
    ).toBeInTheDocument();
  });

  it("publishes when the backend says it is described", () => {
    renderCard();

    expect(screen.getByRole("button", { name: "Start this run" })).toBeEnabled();
  });
});

describe("the card and what was asked for", () => {
  it("leads with the person's own request when the draft records one", () => {
    renderCard({
      reply: {
        ...REPLY,
        draft: {
          ...REPLY.draft,
          intent: {
            request: "Every Monday, sweep my open questions",
            expected_result: "",
            success_example: "",
            empty_input_behavior: "",
            failure_behavior: "",
            trigger: "scheduled",
            assumptions: [],
          },
        },
      },
    });

    expect(
      screen.getByText("You asked for: Every Monday, sweep my open questions"),
    ).toBeInTheDocument();
  });

  it("says nothing about a request nobody recorded", () => {
    renderCard();

    expect(screen.queryByText(/You asked for:/)).not.toBeInTheDocument();
  });
});
