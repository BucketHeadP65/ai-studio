import { screen } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { ArtifactRef } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { ArtifactCard } from "./artifact-card";

// One artifact a run fixed: named up front, read only when asked for, and fetched through the
// one fixed route this app already has. The reference travels as a reference; nothing here ever
// carries a URL, a method, or a body somebody else wrote.

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

describe("ArtifactCard", () => {
  it("names the artifact and reads nothing until it is asked for", () => {
    render(<ArtifactCard runId="run-1" artifact={DRAFT} />);

    expect(screen.getByText("The draft")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show" })).toBeInTheDocument();
    expect(screen.queryByText("net: 400")).not.toBeInTheDocument();
  });

  it("resolves through the run's own resolve route, sending the reference and nothing else", async () => {
    const sent: unknown[] = [];
    server.use(
      http.post(`${BASE}/api/runs/history/run-1/artifacts/resolve`, async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json({ ref: DRAFT, value: "net: 400" });
      }),
    );

    render(<ArtifactCard runId="run-1" artifact={DRAFT} />);
    await userEvent.click(screen.getByRole("button", { name: "Show" }));

    expect(await screen.findByText("net: 400")).toBeInTheDocument();
    expect(sent).toEqual([{ ref: DRAFT }]);
  });

  it("shows a refused reference in the words the host refused it with", async () => {
    server.use(
      http.post(`${BASE}/api/runs/history/run-1/artifacts/resolve`, () =>
        HttpResponse.json(
          {
            ok: false,
            message: "the archived value no longer matches the digest this reference addresses",
          },
          { status: 409 },
        ),
      ),
    );

    render(<ArtifactCard runId="run-1" artifact={DRAFT} />);
    await userEvent.click(screen.getByRole("button", { name: "Show" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "the archived value no longer matches the digest this reference addresses",
    );
  });

  it("reads a JSON artifact as its own formatted value", async () => {
    const json: ArtifactRef = {
      ...DRAFT,
      id: "report",
      label: "The report",
      media_type: "application/json",
    };
    server.use(
      http.post(`${BASE}/api/runs/history/run-1/artifacts/resolve`, () =>
        HttpResponse.json({ ref: json, value: { net: 400 } }),
      ),
    );

    render(<ArtifactCard runId="run-1" artifact={json} />);
    await userEvent.click(screen.getByRole("button", { name: "Show" }));

    expect(await screen.findByText(/"net": 400/)).toBeInTheDocument();
  });
});
