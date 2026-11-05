import { screen, waitFor } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { Trust, TrustCounts, VersionTrust } from "@/api/studio-types";
import { server } from "@/shared/test-support/msw/server";
import { render } from "@/shared/test-support/render";

import { TrustChip } from "./trust-chip";

// The chip over one workflow's earned trust: the level in words, the count of runs behind a
// machine-confirmed level, the names behind a approved one, the total over every saved version
// beside them, and the one act that raises the level -- a person's own approval, through the
// panel this chip opens and posts.

const BASE = "http://studio.test";

function counts(overrides: Partial<TrustCounts> = {}): TrustCounts {
  return {
    runs_with_evidence: 0,
    failed_runs: 0,
    approved_by: [],
    structural_grades: 0,
    structural_mean: null,
    structural_last: null,
    ...overrides,
  };
}

function versionTrust(
  version: number,
  level: string,
  overrides: Partial<TrustCounts> = {},
): VersionTrust {
  return { version, level, ...counts(overrides) };
}

function trust(overrides: Partial<Trust> = {}): Trust {
  return {
    level: "unverified",
    ...counts(),
    total: counts(),
    versions: [versionTrust(1, "unverified")],
    ...overrides,
  };
}

// Finds the one element whose whole text reads `text`, its mono numbers included.
function wholeText(text: string): (content: string, element: Element | null) => boolean {
  return (_content, element) => element?.textContent === text;
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

  it("shows the total runs with evidence across every version beside the current level", () => {
    render(
      <TrustChip
        workflowId="wf.sales.v4"
        trust={trust({
          level: "machine-confirmed",
          ...counts({ runs_with_evidence: 2 }),
          total: counts({ runs_with_evidence: 37, failed_runs: 3, approved_by: ["owner"] }),
          versions: [
            versionTrust(1, "approved", { runs_with_evidence: 20, approved_by: ["owner"] }),
            versionTrust(2, "machine-confirmed", { runs_with_evidence: 9, failed_runs: 2 }),
            versionTrust(3, "machine-confirmed", { runs_with_evidence: 6, failed_runs: 1 }),
            versionTrust(4, "machine-confirmed", { runs_with_evidence: 2 }),
          ],
        })}
        onApproved={vi.fn()}
      />,
    );

    expect(screen.getByText("confirmed by runs")).toBeInTheDocument();
    expect(screen.getByText("2")).toHaveClass("font-mono");
    expect(screen.getByText(wholeText("total 37 runs across 4 versions"))).toBeInTheDocument();
    expect(screen.queryByText(/^failed/)).not.toBeInTheDocument();
  });

  it("keeps a fresh version unverified beside a long trusted total, and offers its approval", () => {
    render(
      <TrustChip
        workflowId="wf.sales.v2"
        trust={trust({
          level: "unverified",
          total: counts({ runs_with_evidence: 37, approved_by: ["owner"] }),
          versions: [
            versionTrust(1, "approved", { runs_with_evidence: 37, approved_by: ["owner"] }),
            versionTrust(2, "unverified"),
          ],
        })}
        onApproved={vi.fn()}
      />,
    );

    expect(screen.getByText("not verified")).toBeInTheDocument();
    expect(screen.queryByText("approved")).not.toBeInTheDocument();
    expect(screen.queryByText("by owner")).not.toBeInTheDocument();
    expect(screen.getByText(wholeText("total 37 runs across 2 versions"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve it" })).toBeInTheDocument();
  });

  it("says no total while the workflow has a single version, whose total is its own count", () => {
    render(
      <TrustChip
        workflowId="wf.sales.v1"
        trust={trust({
          level: "machine-confirmed",
          ...counts({ runs_with_evidence: 7 }),
          total: counts({ runs_with_evidence: 7 }),
          versions: [versionTrust(1, "machine-confirmed", { runs_with_evidence: 7 })],
        })}
        onApproved={vi.fn()}
      />,
    );

    expect(screen.getByText("7")).toHaveClass("font-mono");
    expect(screen.queryByText(/across/)).not.toBeInTheDocument();
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
