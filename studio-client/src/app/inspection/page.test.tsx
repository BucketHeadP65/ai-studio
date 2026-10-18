import { screen, within } from "@testing-library/dom";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { server } from "@/test-support/msw/server";
import { render } from "@/test-support/render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

// Imported after the mock so the page sees the mocked module.
import InspectionPage from "./page";

const BASE = "http://studio.test";

function finding(overrides: Record<string, unknown> = {}) {
  return {
    finding_id: 1,
    lead_id: 1,
    question_id: 1,
    source: "record:harness-author-d01d1796",
    symptom: "warning_ignored",
    symptom_text: "the transcript shows a warning the agent never acted on",
    explanation: "The agent saw a warning in the tool's return and answered as though it never happened.",
    found_by: "inspector",
    model: "claude-test",
    decided_at: "2026-09-10T12:17:00+00:00",
    blames: [
      {
        root_cause: "tool_design",
        root_cause_text: "a tool's own description asked for a check its code never made",
        surface_kind: "tool",
        surface_name: "write_note",
        why: "it promised a check",
      },
    ],
    lead_found_by: "duplicate_call",
    ...overrides,
  };
}

describe("InspectionPage", () => {
  it("shows the count line and one row per finding with its symptom, its run, its detector, its model, its surfaces and its question", async () => {
    server.use(http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ findings: [finding()] })));
    render(<InspectionPage />);

    expect(await screen.findByText(/^1 finding, \d in the last day, 1 run$/)).toBeInTheDocument();
    const row = screen.getByRole("row", { name: /warning ignored/ });
    expect(within(row).getByRole("link", { name: /warning ignored/ })).toHaveAttribute("href", "/inspection/1");
    expect(row).toHaveTextContent("the transcript shows a warning the agent never acted on");
    expect(row).toHaveTextContent("harness-author");
    expect(row).toHaveTextContent("d01d1796");
    expect(row).not.toHaveTextContent("record:");
    expect(row).toHaveTextContent("inspector");
    expect(row).toHaveTextContent("claude-test");
    expect(within(row).getByText("write_note")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "Read" })).toHaveAttribute("href", "/inspection/1#trace");
  });

  it("shows a rule's key under found by, no model and no question for a finding a rule decided", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings`, () =>
        HttpResponse.json({ findings: [finding({ found_by: "rule", lead_found_by: "duplicate_call", model: null, question_id: null })] }),
      ),
    );
    render(<InspectionPage />);

    const row = await screen.findByRole("row", { name: /warning ignored/ });
    expect(row).toHaveTextContent("rule");
    expect(row).toHaveTextContent("duplicate_call");
    expect(within(row).queryByRole("link", { name: "Read" })).not.toBeInTheDocument();
    expect(within(row).getAllByText("none")).toHaveLength(2);
  });

  it("narrows the rows with the symptom select", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings`, () =>
        HttpResponse.json({ findings: [finding(), finding({ finding_id: 2, symptom: "duplicate_call" })] }),
      ),
    );
    render(<InspectionPage />);

    expect(await screen.findAllByRole("row", { name: /ignored|duplicate/ })).toHaveLength(2);
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Symptom" }), "duplicate_call");

    expect(screen.getAllByRole("row", { name: /ignored|duplicate/ })).toHaveLength(1);
    expect(screen.getByRole("row", { name: /duplicate call/ })).toBeInTheDocument();
  });

  it("offers every run of the list as agent and short id, and narrows by it", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings`, () =>
        HttpResponse.json({ findings: [finding(), finding({ finding_id: 2, source: "record:eval-scribe-b3c2d91e" })] }),
      ),
    );
    render(<InspectionPage />);

    await screen.findAllByRole("row", { name: /warning ignored/ });
    const run = screen.getByRole("combobox", { name: "Run or agent" });
    expect(within(run).getByRole("option", { name: "eval-scribe b3c2d91e" })).toBeInTheDocument();
    await userEvent.selectOptions(run, "eval-scribe-b3c2d91e");

    expect(screen.getAllByRole("row", { name: /warning ignored/ })).toHaveLength(1);
  });

  it("asks for the newest 200 once", async () => {
    const asked: string[] = [];
    server.use(
      http.get(`${BASE}/api/inspection/findings`, ({ request }) => {
        asked.push(new URL(request.url).search);
        return HttpResponse.json({ findings: [] });
      }),
    );
    render(<InspectionPage />);

    expect(await screen.findByText("Nothing found yet.")).toBeInTheDocument();
    expect(asked).toEqual(["?limit=200"]);
  });

  it("shows an error sentence when the findings cannot be read", async () => {
    server.use(
      http.get(`${BASE}/api/inspection/findings`, () => HttpResponse.json({ message: "down" }, { status: 500 })),
    );
    render(<InspectionPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read the findings.");
  });
});
