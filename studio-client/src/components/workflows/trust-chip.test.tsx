import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { Trust } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { TrustChip } from "./trust-chip";

// The chip over one workflow's earned trust: the level in words, the count of runs behind a
// machine-confirmed level, the names behind a approved one, and the one act that raises the
// level -- a person's own approval, through the panel this chip opens and posts.

const BASE = "http://studio.test";

function trust(overrides: Partial<Trust> = {}): Trust {
  return {
    level: "unverified",
    runs_with_evidence: 0,
    failed_runs: 0,
    approved_by: [],
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
    ...overrides,
  };
}

describe("TrustChip", () => {
  it("shows an unverified level in words, with the approve button offered", () => {
    render(<TrustChip workflowId="wf.sales.v1" trust={trust()} onApproved={vi.fn()} />);

    expect(screen.getByText("not verified")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve it" })).toBeInTheDocument();
  });

  it("shows a machine-confirmed level with the mono run count, and the approve button offered", () => {
    render(
      <TrustChip
        workflowId="wf.sales.v1"
        trust={trust({ level: "machine-confirmed", runs_with_evidence: 7 })}
        onApproved={vi.fn()}
      />,
    );

    expect(screen.getByText("confirmed by runs")).toBeInTheDocument();
    const count = screen.getByText("7");
    expect(count.tagName).toBe("SPAN");
    expect(count).toHaveClass("font-mono");
    expect(screen.getByText("runs")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve it" })).toBeInTheDocument();
  });

  it("shows a approved level with the names in visible text, and no approve button", () => {
    render(
      <TrustChip
        workflowId="wf.sales.v1"
        trust={trust({ level: "approved", approved_by: ["alex", "owner"] })}
        onApproved={vi.fn()}
      />,
    );

    expect(screen.getByText("approved")).toBeInTheDocument();
    expect(screen.getByText("by alex, owner")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve it" })).not.toBeInTheDocument();
  });

  it("opens the panel with its sentence, then posts the typed name and note and reports the returned trust", async () => {
    let posted: unknown = null;
    server.use(
      http.post(`${BASE}/api/workflows/wf.sales.v1/approve`, async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({
          ok: true,
          trust: trust({ level: "approved", approved_by: ["alex"] }),
        });
      }),
    );
    const onApproved = vi.fn();
    render(<TrustChip workflowId="wf.sales.v1" trust={trust()} onApproved={onApproved} />);

    await userEvent.click(screen.getByRole("button", { name: "Approve it" }));
    expect(
      screen.getByText(
        "Your name goes on record as approving this workflow. There are no accounts here, so nothing fills it in for you.",
      ),
    ).toBeInTheDocument();

    await userEvent.type(screen.getByRole("textbox", { name: "Your name" }), "alex");
    await userEvent.type(screen.getByRole("textbox", { name: "Note (optional)" }), "looks right");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(posted).toEqual({ by: "alex", note: "looks right" }));
    await waitFor(() =>
      expect(onApproved).toHaveBeenCalledWith(
        trust({ level: "approved", approved_by: ["alex"] }),
      ),
    );
  });

  it("asks for a name rather than filling one in, and will not approve until one is typed", async () => {
    let asked = 0;
    server.use(
      http.post(`${BASE}/api/workflows/wf.sales.v1/approve`, () => {
        asked += 1;
        return HttpResponse.json({ ok: true, trust: trust({ level: "approved" }) });
      }),
    );
    render(<TrustChip workflowId="wf.sales.v1" trust={trust()} onApproved={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Approve it" }));

    expect(screen.getByRole("textbox", { name: "Your name" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();

    await userEvent.type(screen.getByRole("textbox", { name: "Your name" }), "   ");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(asked).toBe(0);

    await userEvent.type(screen.getByRole("textbox", { name: "Your name" }), "alex");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("shows the API's own error message in the panel without closing it", async () => {
    server.use(
      http.post(`${BASE}/api/workflows/wf.sales.v1/approve`, () =>
        HttpResponse.json({ detail: "an approval needs a name" }, { status: 400 }),
      ),
    );
    render(<TrustChip workflowId="wf.sales.v1" trust={trust()} onApproved={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Approve it" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Your name" }), "alex");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("an approval needs a name")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });
});
